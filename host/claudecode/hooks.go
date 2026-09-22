// Package claudecode holds both ends of the Claude Code hook: the route the
// bastion serves, and the client Claude Code runs on every turn.
package claudecode

import (
	"context"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"strconv"
	"time"

	"github.com/chrisguidry/agentic-memory/host/recall"
	"github.com/chrisguidry/agentic-memory/host/transcripts"
)

// Harness is what this harness is called everywhere the service records it.
const Harness = "claude-code"

// payload is the part of Claude Code's hook payload the bastion reads. The
// rest goes unread, because the bastion adds nothing the service cannot get
// from the transcript.
type payload struct {
	SessionID      string `json:"session_id"`
	HookEventName  string `json:"hook_event_name"`
	Cwd            string `json:"cwd"`
	Prompt         string `json:"prompt"`
	TranscriptPath string `json:"transcript_path"`
}

// Hooks answers `POST /claude-code/hooks`.
//
// The response body is what the hook prints to stdout, and the status is
// always 200. On `UserPromptSubmit` with memory to add, the body is the
// envelope Claude Code reads as context. On every other event, and on a turn
// with no memory, it is empty.
type Hooks struct {
	Recall   *recall.Client
	Shipper  *transcripts.Shipper
	Scope    func(cwd string) (key, kind string)
	Machine  string
	Limit    int
	Deadline time.Duration
	Now      func() time.Time
	Log      *log.Logger
}

func (h *Hooks) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	raw, err := io.ReadAll(io.LimitReader(r.Body, 8<<20))
	if err != nil {
		answer(w, "")
		return
	}
	var event payload
	if err := json.Unmarshal(raw, &event); err != nil {
		h.logf("could not read the hook payload: %v", err)
		answer(w, "")
		return
	}

	body := ""
	if event.HookEventName == "UserPromptSubmit" {
		body = envelope(h.block(r.Context(), event))
	}
	answer(w, body)

	// The turn is over as far as Claude Code is concerned, so the file can be
	// read now. Reading it before the response is written would put the whole
	// read inside the turn.
	if ships(event.HookEventName) && event.TranscriptPath != "" {
		h.ship(event)
	}
}

// ships reports whether this event means the transcript gained lines. Claude
// Code fires these four, and the bastion reads the file after each one.
func ships(event string) bool {
	switch event {
	case "UserPromptSubmit", "Stop", "SubagentStop", "SessionEnd":
		return true
	}
	return false
}

// block asks the service what earlier sessions said, inside the deadline the
// turn is waiting through. A missing service, a slow one, and an empty answer
// all look the same from the turn: no memory this time.
func (h *Hooks) block(ctx context.Context, event payload) string {
	if event.Cwd == "" || event.SessionID == "" {
		h.logf("no working directory or session in the payload")
		return ""
	}
	key, _ := h.Scope(event.Cwd)

	ctx, cancel := context.WithTimeout(ctx, h.Deadline)
	defer cancel()
	statements, err := h.Recall.Statements(ctx, recall.Ask{
		SessionID: event.SessionID,
		Harness:   Harness,
		ScopeKey:  key,
		Prompt:    event.Prompt,
		Limit:     h.Limit,
	})
	if err != nil {
		h.logf("%s: no answer within %s: %v", event.SessionID, h.Deadline, err)
		return ""
	}
	if len(statements) == 0 {
		return ""
	}
	h.logf("%s: %d statements for %s", event.SessionID, len(statements), key)
	return recall.Block(statements, h.now())
}

func (h *Hooks) ship(event payload) {
	// The client is gone by now, so the shipment gets a deadline of its own
	// rather than the request's.
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	if _, err := h.Shipper.Ship(ctx, transcripts.Request{
		Harness: Harness,
		Path:    event.TranscriptPath,
		Machine: h.Machine,
		Cwd:     event.Cwd,
	}); err != nil {
		h.logf("could not ship %s: %v", event.TranscriptPath, err)
	}
}

func (h *Hooks) now() time.Time {
	if h.Now != nil {
		return h.Now()
	}
	return time.Now().UTC()
}

func (h *Hooks) logf(format string, arguments ...any) {
	if h.Log != nil {
		h.Log.Printf(format, arguments...)
	}
}

// envelope wraps the block in the shape the hook docs give for adding context
// on `UserPromptSubmit`. A plain line on stdout is also taken as context, and
// the envelope is used so that nothing about the block is left to a parser.
// A turn with no memory gets no envelope, because an empty stdout is how the
// hook says it has nothing to add.
func envelope(block string) string {
	if block == "" {
		return ""
	}
	wrapped, err := json.Marshal(map[string]any{
		"hookSpecificOutput": map[string]any{
			"hookEventName":     "UserPromptSubmit",
			"additionalContext": block,
		},
	})
	if err != nil {
		return ""
	}
	return string(wrapped)
}

// answer writes the whole response and pushes it to the client. The length is
// set so the client reads the body by it and never waits for a close, and the
// flush is what makes the response complete before the caller reads a
// transcript. The client copies these bytes to stdout and reads none of them.
func answer(w http.ResponseWriter, body string) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Header().Set("Content-Length", strconv.Itoa(len(body)))
	w.WriteHeader(http.StatusOK)
	io.WriteString(w, body)
	if flusher, ok := w.(http.Flusher); ok {
		flusher.Flush()
	}
}
