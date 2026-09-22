package transcripts_test

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/chrisguidry/agentic-memory/host/transcripts"
)

// shipment is the body the plan names for `POST /v1/transcripts`, declared
// here so the test reads the wire rather than the shipper's own type.
type shipment struct {
	Harness       string   `json:"harness"`
	Machine       string   `json:"machine"`
	Path          string   `json:"path"`
	Cwd           string   `json:"cwd"`
	Version       string   `json:"version"`
	Scope         string   `json:"scope"`
	ScopeKind     string   `json:"scope_kind"`
	EntriesBefore int      `json:"entries_before"`
	Lines         []string `json:"lines"`
}

// service answers `POST /v1/transcripts` and keeps what it was sent. It
// refuses while `refusing` is set, which is how a service that is down looks
// from the bastion.
type service struct {
	*httptest.Server
	mu        sync.Mutex
	shipments []shipment
	refusing  bool
}

func newService(t *testing.T) *service {
	t.Helper()
	found := &service{}
	found.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/transcripts" {
			http.NotFound(w, r)
			return
		}
		var sent shipment
		if err := json.NewDecoder(r.Body).Decode(&sent); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		found.mu.Lock()
		defer found.mu.Unlock()
		if found.refusing {
			http.Error(w, "away", http.StatusServiceUnavailable)
			return
		}
		found.shipments = append(found.shipments, sent)
		fmt.Fprintf(w, `{"received": %d, "inserted": %d, "repeated": 1}`, len(sent.Lines), len(sent.Lines)-1)
	}))
	t.Cleanup(found.Close)
	return found
}

func (s *service) refuse(refusing bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.refusing = refusing
}

func (s *service) sent() []shipment {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]shipment(nil), s.shipments...)
}

func newShipper(t *testing.T, at *service) *transcripts.Shipper {
	t.Helper()
	return &transcripts.Shipper{
		Service:  at.URL,
		StateDir: t.TempDir(),
		HTTP:     at.Client(),
		Scope:    func(string) (string, string) { return "example.test/acme/widget", "repo" },
	}
}

// entry is one invented transcript line.
func entry(number int) string {
	return fmt.Sprintf(`{"uuid":"entry-%d","cwd":"/work/widget","version":"9.9.9","text":"line %d"}`, number, number)
}

// transcript writes the first `count` entries to a new file and returns its
// path.
func transcript(t *testing.T, count int) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "session.jsonl")
	write(t, path, count)
	return path
}

func write(t *testing.T, path string, count int) {
	t.Helper()
	lines := make([]string, 0, count)
	for number := 1; number <= count; number++ {
		lines = append(lines, entry(number))
	}
	body := ""
	if count > 0 {
		body = strings.Join(lines, "\n") + "\n"
	}
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

func appendLines(t *testing.T, path string, from, to int) {
	t.Helper()
	file, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	for number := from; number <= to; number++ {
		if _, err := fmt.Fprintln(file, entry(number)); err != nil {
			t.Fatal(err)
		}
	}
}

func ship(t *testing.T, shipper *transcripts.Shipper, path string) error {
	t.Helper()
	_, err := shipper.Ship(context.Background(), transcripts.Request{
		Harness: "claude-code",
		Path:    path,
		Machine: "laptop",
		Cwd:     "/work/widget",
	})
	return err
}

// counted is what the shipper answered, which is what a caller of
// `POST /transcripts/ship` reads.
func counted(t *testing.T, shipper *transcripts.Shipper, path string) transcripts.Counts {
	t.Helper()
	answer, err := shipper.Ship(context.Background(), transcripts.Request{
		Harness: "claude-code",
		Path:    path,
		Machine: "laptop",
		Cwd:     "/work/widget",
	})
	if err != nil {
		t.Fatal(err)
	}
	var found transcripts.Counts
	if err := json.Unmarshal(answer, &found); err != nil {
		t.Fatal(err)
	}
	return found
}

func TestTheAnswerCountsEveryBatchTheFileTook(t *testing.T) {
	at := newService(t)
	shipper := newShipper(t, at)
	// Two batches, so a caller that reads only the last one sees 50 lines of
	// a file that had 250.
	path := transcript(t, transcripts.Batch+50)

	found := counted(t, shipper, path)
	want := transcripts.Counts{
		Received: transcripts.Batch + 50,
		Inserted: transcripts.Batch + 50 - 2,
		Repeated: 2,
	}
	if found != want {
		t.Errorf("got %+v, want %+v", found, want)
	}
}

func TestAFileWithNothingNewIsCountedAsNothing(t *testing.T) {
	at := newService(t)
	shipper := newShipper(t, at)
	path := transcript(t, 2)

	counted(t, shipper, path)
	if found := counted(t, shipper, path); found != (transcripts.Counts{}) {
		t.Errorf("got %+v, want nothing", found)
	}
}

func TestAFileGrowsAndOnlyTheNewLinesAreSent(t *testing.T) {
	at := newService(t)
	shipper := newShipper(t, at)
	path := transcript(t, 2)

	if err := ship(t, shipper, path); err != nil {
		t.Fatal(err)
	}
	appendLines(t, path, 3, 5)
	if err := ship(t, shipper, path); err != nil {
		t.Fatal(err)
	}

	sent := at.sent()
	if len(sent) != 2 {
		t.Fatalf("got %d shipments, want 2", len(sent))
	}
	if sent[0].EntriesBefore != 0 || len(sent[0].Lines) != 2 {
		t.Errorf("got %d entries before %d lines, want 0 before 2", sent[0].EntriesBefore, len(sent[0].Lines))
	}
	if sent[1].EntriesBefore != 2 || len(sent[1].Lines) != 3 {
		t.Errorf("got %d entries before %d lines, want 2 before 3", sent[1].EntriesBefore, len(sent[1].Lines))
	}
	if sent[1].Lines[0] != entry(3) {
		t.Errorf("got %q as the first new line, want %q", sent[1].Lines[0], entry(3))
	}
}

func TestAFileWithNothingNewSendsNothing(t *testing.T) {
	at := newService(t)
	shipper := newShipper(t, at)
	path := transcript(t, 2)

	for range 2 {
		if err := ship(t, shipper, path); err != nil {
			t.Fatal(err)
		}
	}
	if sent := at.sent(); len(sent) != 1 {
		t.Fatalf("got %d shipments, want 1", len(sent))
	}
}

func TestALineStillBeingWrittenWaitsForItsNewline(t *testing.T) {
	at := newService(t)
	shipper := newShipper(t, at)
	path := transcript(t, 2)

	file, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := file.WriteString(`{"uuid":"entry-3","tex`); err != nil {
		t.Fatal(err)
	}
	file.Close()

	if err := ship(t, shipper, path); err != nil {
		t.Fatal(err)
	}
	sent := at.sent()
	if len(sent[0].Lines) != 2 {
		t.Fatalf("got %d lines, want 2", len(sent[0].Lines))
	}
}

func TestAShorterFileStartsOver(t *testing.T) {
	at := newService(t)
	shipper := newShipper(t, at)
	path := transcript(t, 5)

	if err := ship(t, shipper, path); err != nil {
		t.Fatal(err)
	}
	write(t, path, 2)
	if err := ship(t, shipper, path); err != nil {
		t.Fatal(err)
	}

	sent := at.sent()
	if len(sent) != 2 {
		t.Fatalf("got %d shipments, want 2", len(sent))
	}
	if sent[1].EntriesBefore != 0 || len(sent[1].Lines) != 2 {
		t.Errorf("got %d entries before %d lines, want 0 before 2", sent[1].EntriesBefore, len(sent[1].Lines))
	}
}

func TestAChangedHeadStartsOver(t *testing.T) {
	at := newService(t)
	shipper := newShipper(t, at)
	path := transcript(t, 3)

	if err := ship(t, shipper, path); err != nil {
		t.Fatal(err)
	}
	// A rewrite that is no shorter, so only the fingerprint catches it.
	replaced := strings.ReplaceAll(readAll(t, path), "line ", "other ")
	if err := os.WriteFile(path, []byte(replaced), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := ship(t, shipper, path); err != nil {
		t.Fatal(err)
	}

	sent := at.sent()
	if sent[1].EntriesBefore != 0 || len(sent[1].Lines) != 3 {
		t.Errorf("got %d entries before %d lines, want 0 before 3", sent[1].EntriesBefore, len(sent[1].Lines))
	}
}

func TestTheOffsetAdvancesOnlyOnSuccess(t *testing.T) {
	at := newService(t)
	shipper := newShipper(t, at)
	path := transcript(t, 3)

	at.refuse(true)
	if err := ship(t, shipper, path); err == nil {
		t.Fatal("got no error from a service that refused")
	}
	at.refuse(false)
	if err := ship(t, shipper, path); err != nil {
		t.Fatal(err)
	}

	sent := at.sent()
	if len(sent) != 1 {
		t.Fatalf("got %d shipments, want 1", len(sent))
	}
	if sent[0].EntriesBefore != 0 || len(sent[0].Lines) != 3 {
		t.Errorf("got %d entries before %d lines, want 0 before 3", sent[0].EntriesBefore, len(sent[0].Lines))
	}
}

func TestLongFilesGoInBatches(t *testing.T) {
	at := newService(t)
	shipper := newShipper(t, at)
	path := transcript(t, transcripts.Batch+50)

	if err := ship(t, shipper, path); err != nil {
		t.Fatal(err)
	}
	sent := at.sent()
	if len(sent) != 2 {
		t.Fatalf("got %d shipments, want 2", len(sent))
	}
	if len(sent[0].Lines) != transcripts.Batch || sent[0].EntriesBefore != 0 {
		t.Errorf("got %d lines at %d, want %d at 0", len(sent[0].Lines), sent[0].EntriesBefore, transcripts.Batch)
	}
	if len(sent[1].Lines) != 50 || sent[1].EntriesBefore != transcripts.Batch {
		t.Errorf("got %d lines at %d, want 50 at %d", len(sent[1].Lines), sent[1].EntriesBefore, transcripts.Batch)
	}
}

func TestAnotherHarnessShipsTheWholeFileEveryTime(t *testing.T) {
	at := newService(t)
	shipper := newShipper(t, at)
	path := transcript(t, 2)

	for range 2 {
		if _, err := shipper.Ship(context.Background(), transcripts.Request{
			Harness: "codex",
			Path:    path,
			Machine: "laptop",
			Cwd:     "/work/widget",
		}); err != nil {
			t.Fatal(err)
		}
	}

	sent := at.sent()
	if len(sent) != 2 {
		t.Fatalf("got %d shipments, want 2", len(sent))
	}
	for number, found := range sent {
		if found.EntriesBefore != 0 || len(found.Lines) != 2 {
			t.Errorf("shipment %d: got %d entries before %d lines, want 0 before 2", number, found.EntriesBefore, len(found.Lines))
		}
	}
}

func TestTheShipmentCarriesTheScopeAndTheFirstCwdAndVersion(t *testing.T) {
	at := newService(t)
	shipper := newShipper(t, at)
	path := transcript(t, 1)

	if err := ship(t, shipper, path); err != nil {
		t.Fatal(err)
	}
	// Later entries carry neither, so the offset has to remember the first.
	if err := os.WriteFile(path, []byte(readAll(t, path)+"{\"uuid\":\"entry-2\"}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := ship(t, shipper, path); err != nil {
		t.Fatal(err)
	}

	sent := at.sent()
	for number, found := range sent {
		if found.Cwd != "/work/widget" || found.Version != "9.9.9" {
			t.Errorf("shipment %d: got cwd %q version %q", number, found.Cwd, found.Version)
		}
		if found.Scope != "example.test/acme/widget" || found.ScopeKind != "repo" {
			t.Errorf("shipment %d: got scope %q %q", number, found.Scope, found.ScopeKind)
		}
		if found.Harness != "claude-code" || found.Machine != "laptop" || found.Path != path {
			t.Errorf("shipment %d: got %q %q %q", number, found.Harness, found.Machine, found.Path)
		}
	}
}

func readAll(t *testing.T, path string) string {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(raw)
}
