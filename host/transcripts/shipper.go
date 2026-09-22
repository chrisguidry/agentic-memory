// Package transcripts sends the raw lines a harness's transcript gained to the
// service, which parses them with the reader it already has.
//
// The bastion ships bytes, not records, and never learns a transcript format.
// It remembers an offset and a fingerprint per file. A file that shrinks or
// whose head changes was rewritten, and the bastion starts it over from zero.
package transcripts

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"

	"github.com/chrisguidry/agentic-memory/host/repository"
)

// Batch is how many lines go in one request. A session's transcript can gain
// thousands of lines between events, and the service reads one request whole.
const Batch = 200

// Opener opens a transcript for reading. The bastion opens the real file. A
// test names its own so it can observe when a read begins.
type Opener func(path string) (io.ReadSeekCloser, error)

// Request names one file to ship. Backfill sends the same shape over the
// socket, once per file.
type Request struct {
	Harness string `json:"harness"`
	Path    string `json:"path"`
	Machine string `json:"machine"`
	Cwd     string `json:"cwd"`
}

// Counts is what the service reports for what it was sent. A file that goes
// in several batches is counted across all of them, so a caller sees the whole
// file.
type Counts struct {
	Received int `json:"received"`
	Inserted int `json:"inserted"`
	Repeated int `json:"repeated"`
}

func (c *Counts) add(found Counts) {
	c.Received += found.Received
	c.Inserted += found.Inserted
	c.Repeated += found.Repeated
}

// shipment is the body of `POST /v1/transcripts`.
type shipment struct {
	Harness       string                 `json:"harness"`
	Machine       string                 `json:"machine"`
	Path          string                 `json:"path"`
	Cwd           string                 `json:"cwd"`
	Version       string                 `json:"version,omitempty"`
	Scope         string                 `json:"scope"`
	ScopeKind     string                 `json:"scope_kind"`
	Repository    *repository.Repository `json:"repository,omitempty"`
	EntriesBefore int                    `json:"entries_before"`
	Lines         []string               `json:"lines"`
}

// Shipper sends transcripts to one service and remembers where it read each
// one to.
type Shipper struct {
	Service       string
	Authorization string
	StateDir      string
	HTTP          *http.Client
	Log           *log.Logger

	// Open opens a transcript. A zero Shipper opens the real file.
	Open Opener
	// Scope derives the scope of a working directory. A test names its own so
	// its expectations do not depend on where the test directory is.
	Scope func(cwd string) (key, kind string)
	// Repositories reads what git says about a working directory. A zero
	// Shipper sends no repository.
	Repositories *repository.Reader

	files   sync.Map // path -> *sync.Mutex
	pending sync.Map // path -> *attempt
}

// Ship sends what a file gained since the bastion last saw it, and returns the
// counts the service reported across every batch. Two ships of one file never
// run at once, because each would start from the offset the other is about to
// advance.
func (s *Shipper) Ship(ctx context.Context, request Request) ([]byte, error) {
	guard, _ := s.files.LoadOrStore(request.Path, &sync.Mutex{})
	lock := guard.(*sync.Mutex)
	lock.Lock()
	defer lock.Unlock()

	answer, err := s.ship(ctx, request)
	if err != nil {
		s.failed(request, err)
		return nil, err
	}
	s.delivered(request.Path)
	return answer, nil
}

func (s *Shipper) ship(ctx context.Context, request Request) ([]byte, error) {
	name := filepath.Base(request.Path)

	// Only Claude Code's live hook fires many times on a growing file. Every
	// other harness ships whole, so its shipment starts at the first entry.
	tracked := request.Harness == "claude-code"
	var state offset
	stateFile := offsetFile(s.StateDir, request.Path)
	if tracked {
		state = readOffset(stateFile)
	}

	file, err := s.open(request.Path)
	if err != nil {
		return nil, err
	}
	defer file.Close()

	chunk, state, err := s.unread(file, state, name)
	if err != nil {
		return nil, err
	}
	if len(chunk) == 0 {
		return json.Marshal(Counts{})
	}

	lines := split(chunk)
	cwd := firstValue(lines, "cwd")
	if cwd == "" {
		cwd = state.Cwd
	}
	if cwd == "" {
		cwd = request.Cwd
	}
	if cwd == "" {
		s.logf("%s: waiting for an entry with a working directory", name)
		return json.Marshal(Counts{})
	}
	version := firstValue(lines, "version")
	if version == "" {
		version = state.Version
	}

	key, kind := s.scopeOf(cwd)
	// git runs here and never on the recall path, because this runs after the
	// response to the turn is already written.
	var found *repository.Repository
	if s.Repositories != nil {
		found = s.Repositories.Of(cwd)
	}
	from := state.Offset
	before := state.Entries
	var counted Counts
	for start := 0; start < len(lines); start += Batch {
		stop := min(start+Batch, len(lines))
		batch := lines[start:stop]
		found, err := s.post(ctx, shipment{
			Harness:       request.Harness,
			Machine:       request.Machine,
			Path:          request.Path,
			Cwd:           cwd,
			Version:       version,
			Scope:         key,
			ScopeKind:     kind,
			Repository:    found,
			EntriesBefore: before,
			Lines:         batch,
		})
		if err != nil {
			return nil, err
		}
		counted.add(found)
		before += entriesIn(batch)
	}

	if tracked {
		state.Path = request.Path
		state.Offset += int64(len(chunk))
		state.Entries = before
		state.Cwd, state.Version = cwd, version
		if state.Head, err = fingerprint(file, state.Offset); err != nil {
			return nil, err
		}
		if err := writeOffset(stateFile, state); err != nil {
			return nil, err
		}
	}
	s.logf("%s: shipped %d lines from byte %d", name, len(lines), from)
	return json.Marshal(counted)
}

// unread returns the whole lines after the offset, and the offset they start
// at. A file that shrank or whose head changed was rewritten, so it starts
// over from zero.
func (s *Shipper) unread(file io.ReadSeeker, state offset, name string) ([]byte, offset, error) {
	size, err := file.Seek(0, io.SeekEnd)
	if err != nil {
		return nil, state, err
	}
	if state.Offset > 0 {
		found, err := fingerprint(file, state.Offset)
		if err != nil {
			return nil, state, err
		}
		if size < state.Offset || found != state.Head {
			s.logf("%s: rewritten, starting over", name)
			state.Offset, state.Entries = 0, 0
		}
	}
	if _, err := file.Seek(state.Offset, io.SeekStart); err != nil {
		return nil, state, err
	}
	chunk, err := io.ReadAll(file)
	if err != nil {
		return nil, state, err
	}
	// A line still being written ends without a newline, and a partial entry
	// is not an entry. It is read on the next event.
	complete := bytes.LastIndexByte(chunk, '\n')
	if complete < 0 {
		return nil, state, nil
	}
	return chunk[:complete+1], state, nil
}

func (s *Shipper) post(ctx context.Context, body shipment) (Counts, error) {
	var counted Counts
	raw, err := json.Marshal(body)
	if err != nil {
		return counted, err
	}
	address := strings.TrimSuffix(s.Service, "/") + "/v1/transcripts"
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, address, bytes.NewReader(raw))
	if err != nil {
		return counted, err
	}
	request.Header.Set("Content-Type", "application/json")
	if s.Authorization != "" {
		request.Header.Set("Authorization", s.Authorization)
	}
	response, err := s.HTTP.Do(request)
	if err != nil {
		return counted, err
	}
	defer response.Body.Close()
	answer, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if err != nil {
		return counted, err
	}
	// The offset advances on a 2xx and on nothing else, so a refusal here
	// leaves the file exactly where the last accepted shipment ended.
	if response.StatusCode < 200 || response.StatusCode > 299 {
		return counted, fmt.Errorf("the service answered %s", response.Status)
	}
	if err := json.Unmarshal(answer, &counted); err != nil {
		return counted, err
	}
	return counted, nil
}

func (s *Shipper) open(path string) (io.ReadSeekCloser, error) {
	if s.Open != nil {
		return s.Open(path)
	}
	return os.Open(path)
}

func (s *Shipper) scopeOf(cwd string) (string, string) {
	if s.Scope != nil {
		return s.Scope(cwd)
	}
	return "", "directory"
}

func (s *Shipper) logf(format string, arguments ...any) {
	if s.Log != nil {
		s.Log.Printf(format, arguments...)
	}
}

// split gives the chunk's whole lines. The chunk ends in a newline, so the
// empty string after the last one is dropped.
func split(chunk []byte) []string {
	lines := strings.Split(string(chunk), "\n")
	return lines[:len(lines)-1]
}

// firstValue is the value of key on the first line that carries it.
func firstValue(lines []string, key string) string {
	for _, line := range lines {
		var entry map[string]any
		if err := json.Unmarshal([]byte(line), &entry); err != nil {
			continue
		}
		if found, ok := entry[key].(string); ok && found != "" {
			return found
		}
	}
	return ""
}

// entriesIn counts the lines the service's reader will number, by the reader's
// own rule: a line that is a JSON object takes a place in the file.
func entriesIn(lines []string) int {
	found := 0
	for _, line := range lines {
		var entry map[string]any
		if err := json.Unmarshal([]byte(line), &entry); err == nil && entry != nil {
			found++
		}
	}
	return found
}
