package claudecode

import (
	"encoding/json"
	"fmt"
	"net/http"
	"slices"
	"sync"

	"github.com/chrisguidry/agentic-memory/host/recall"
)

// ProbePath is the route a probe session's hook posts to. A probe is handed
// what a live turn would be handed, and the bastion stores nothing of it: no
// injection on the service, and no transcript. The offset record it writes
// holds only the probe mark, which keeps every later event from shipping the
// transcript.
//
// The mark is a route of its own, because a bastion built before probes
// existed proxies an unknown path to the service, which answers 404, so the
// probe gets no memory and nothing is stored. The same bastion would ignore a
// header on the live route, and ship the probe's transcript to the store.
const ProbePath = "/claude-code/probes"

// routeFor returns the route the hook posts to for a value of
// AGENTIC_MEMORY_PROBE. `1` and `true` mark a probe, and empty, `0`, and
// `false` mark a live session. Any other value is an error, and the hook
// forwards nothing: a typo read as live would put the probe's prompts in the
// store, and one read as a probe would keep a person's own session out of it.
func routeFor(value string) (string, error) {
	switch value {
	case "1", "true":
		return ProbePath, nil
	case "", "0", "false":
		return "/claude-code/hooks", nil
	}
	return "", fmt.Errorf("AGENTIC_MEMORY_PROBE is %q, which is none of 1, true, 0, false, or empty, "+
		"so this turn has no memory and nothing was sent", value)
}

// Probes answers `POST /claude-code/probes`. The body and the answer are the
// ones `POST /claude-code/hooks` takes and gives.
func (h *Hooks) Probes() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h.serve(w, r, true)
	})
}

// handed is what the bastion handed each probe session, by session id.
//
// The service writes no injection for a probe, so this is the only record of
// what a probe session already has in its conversation. Without it, every
// prompt in a probe session would take the opening form, which is the form of
// a session that was handed nothing yet. When the service's opening limit is
// above 0, that form also hands the top of the scope's list. The record is
// kept in memory only, so what a probe was handed is written nowhere. A
// bastion that restarts loses it, and the next prompt of each probe session
// takes the opening form again.
//
// SessionEnd does not forget a session. Each `claude -p` process ends its
// session when it exits, and `claude -p --resume` goes on with the same one,
// so a probe that forgot at SessionEnd would hand every resumed turn the same
// statements again, and the opening list too when its limit is above 0. The
// bastion keeps the sessions it used last instead, up to `limit`, and forgets
// the one it used longest ago to make room.
type handed struct {
	mu      sync.Mutex
	limit   int
	clock   uint64
	session map[string]*probeSession
}

// probeSessions is how many probe sessions the bastion keeps what it handed.
// A session holds a few dozen ids, so a thousand sessions hold little memory.
const probeSessions = 1000

type probeSession struct {
	ids  map[int64]struct{}
	used uint64
}

// touch marks the session as used now, and returns it, or nil when the
// bastion keeps nothing for it.
func (h *handed) touch(session string) *probeSession {
	found := h.session[session]
	if found != nil {
		h.clock++
		found.used = h.clock
	}
	return found
}

// of returns what the session was handed, in order.
func (h *handed) of(session string) []int64 {
	h.mu.Lock()
	defer h.mu.Unlock()
	kept := h.touch(session)
	if kept == nil {
		return nil
	}
	var found []int64
	for id := range kept.ids {
		found = append(found, id)
	}
	slices.Sort(found)
	return found
}

func (h *handed) add(session string, statements []recall.Statement) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.session == nil {
		h.session = map[string]*probeSession{}
	}
	if h.session[session] == nil {
		h.evict()
		h.session[session] = &probeSession{ids: map[int64]struct{}{}}
	}
	kept := h.touch(session)
	for _, found := range statements {
		kept.ids[found.ID] = struct{}{}
	}
}

// evict forgets the session used longest ago when the bastion keeps as many
// as it may. It reads every session, which at the limit is a thousand
// comparisons once per new probe session.
func (h *handed) evict() {
	limit := h.limit
	if limit == 0 {
		limit = probeSessions
	}
	if len(h.session) < limit {
		return
	}
	var oldest string
	var used uint64
	for session, kept := range h.session {
		if oldest == "" || kept.used < used {
			oldest, used = session, kept.used
		}
	}
	delete(h.session, oldest)
}

// Preflight answers `GET /claude-code/probes` with `{"probes": true}`, so a
// validator can confirm this bastion takes probes before its first probe
// session. A bastion that has no probe route proxies the path to the service,
// which answers 404.
//
// With `?transcript=<path>`, the answer also names what the bastion records
// for that transcript: `probe` for one marked as a probe's, `live` for one it
// ships from, and `none` for one it has never seen. After a probe session's
// first event, anything but `probe` means its transcript can be shipped.
func (h *Hooks) Preflight() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		found := map[string]any{"probes": true}
		if path := r.URL.Query().Get("transcript"); path != "" {
			found["record"] = h.Shipper.Recorded(path)
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(found)
	})
}
