package transcripts_test

import (
	"context"
	"log"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/chrisguidry/agentic-memory/host/transcripts"
)

// clock is the time a test says it is. The shipper reads it for every wait it
// sets, so a test moves through minutes of backoff without sleeping.
type clock struct {
	now time.Time
}

func (c *clock) read() time.Time {
	return c.now
}

// retrying builds a shipper on a clock the test moves, with a journal the test
// reads. Two shippers on one state directory are one bastion before and after
// a restart.
func retrying(t *testing.T, at *service, stateDir string) (*transcripts.Shipper, *clock, *strings.Builder) {
	t.Helper()
	moment := &clock{now: time.Date(2026, 9, 21, 12, 0, 0, 0, time.UTC)}
	journal := &strings.Builder{}
	shipper := &transcripts.Shipper{
		Service:  at.URL,
		StateDir: stateDir,
		HTTP:     at.Client(),
		Log:      log.New(journal, "", 0),
		Now:      moment.read,
		Scope:    func(string) (string, string) { return "example.test/acme/widget", "repo" },
	}
	return shipper, moment, journal
}

// tick moves the clock one second at a time and offers what is due at each,
// which is what the bastion's own ticker does.
func tick(shipper *transcripts.Shipper, moment *clock, through time.Duration) {
	for range int(through / time.Second) {
		moment.now = moment.now.Add(time.Second)
		shipper.RetryDue(context.Background(), moment.now)
	}
}

// unwritten is a transcript path with no file at it yet.
func unwritten(t *testing.T) string {
	t.Helper()
	return filepath.Join(t.TempDir(), "session.jsonl")
}

func TestAFileWrittenWithinTheGracePeriodShips(t *testing.T) {
	at := newService(t)
	shipper, moment, _ := retrying(t, at, t.TempDir())
	path := unwritten(t)

	if err := ship(t, shipper, path); err == nil {
		t.Fatal("got no error for a file that does not exist")
	}
	tick(shipper, moment, transcripts.Grace/2)
	write(t, path, 3)
	tick(shipper, moment, transcripts.Grace)

	if sent := at.sent(); len(sent) != 1 || len(sent[0].Lines) != 3 {
		t.Fatalf("got %d shipments, want 1 of 3 lines", len(sent))
	}
	if behind := shipper.Behind(); behind != 0 {
		t.Errorf("got %d files behind, want 0", behind)
	}
}

func TestAFileThatIsNeverWrittenIsDroppedAfterTheGracePeriod(t *testing.T) {
	at := newService(t)
	stateDir := t.TempDir()
	shipper, moment, journal := retrying(t, at, stateDir)
	path := unwritten(t)

	ship(t, shipper, path)
	tick(shipper, moment, transcripts.Grace+time.Minute)
	tick(shipper, moment, time.Hour)

	if dropped := strings.Count(journal.String(), "dropped"); dropped != 1 {
		t.Errorf("got %d lines that drop the file, want 1:\n%s", dropped, journal)
	}
	if lines := strings.Count(journal.String(), "\n"); lines != 2 {
		t.Errorf("got %d journal lines, want one on the first failure and one on the drop:\n%s", lines, journal)
	}
	if behind := shipper.Behind(); behind != 0 {
		t.Errorf("got %d files behind, want 0", behind)
	}
	if at.tried() != 0 {
		t.Errorf("the service was asked %d times for a file that was never written", at.tried())
	}
	restarted, _, _ := retrying(t, at, stateDir)
	restarted.Resume()
	if behind := restarted.Behind(); behind != 0 {
		t.Errorf("a restart resumed %d dropped files, want 0", behind)
	}
}

func TestAFileThatCannotBeReadIsDroppedAtOnce(t *testing.T) {
	cases := []struct {
		name string
		path func(t *testing.T, shipper *transcripts.Shipper) string
	}{
		{"a directory", func(t *testing.T, _ *transcripts.Shipper) string {
			return t.TempDir()
		}},
		{"a path under a file", func(t *testing.T, _ *transcripts.Shipper) string {
			return filepath.Join(transcript(t, 1), "session.jsonl")
		}},
		{"a file deleted after it shipped", func(t *testing.T, shipper *transcripts.Shipper) string {
			path := transcript(t, 1)
			ship(t, shipper, path)
			if err := os.Remove(path); err != nil {
				t.Fatal(err)
			}
			return path
		}},
	}
	for _, unreadable := range cases {
		t.Run(unreadable.name, func(t *testing.T) {
			at := newService(t)
			shipper, moment, journal := retrying(t, at, t.TempDir())

			ship(t, shipper, unreadable.path(t, shipper))
			tick(shipper, moment, time.Hour)

			if dropped := strings.Count(journal.String(), "dropped"); dropped != 1 {
				t.Errorf("got %d lines that drop the file, want 1:\n%s", dropped, journal)
			}
			if strings.Contains(journal.String(), "waiting") || strings.Contains(journal.String(), "retrying") {
				t.Errorf("the journal waits on a file that cannot be read:\n%s", journal)
			}
			if behind := shipper.Behind(); behind != 0 {
				t.Errorf("got %d files behind, want 0", behind)
			}
		})
	}
}

func TestAServiceFailureBacksOffUntilTheServiceAnswers(t *testing.T) {
	at := newService(t)
	shipper, moment, _ := retrying(t, at, t.TempDir())
	path := transcript(t, 3)

	at.refuse(true)
	ship(t, shipper, path)
	tick(shipper, moment, transcripts.FirstWait-time.Second)
	if tried := at.tried(); tried != 1 {
		t.Fatalf("got %d requests before the first wait passed, want 1", tried)
	}
	tick(shipper, moment, time.Second)
	if tried := at.tried(); tried != 2 {
		t.Fatalf("got %d requests once the first wait passed, want 2", tried)
	}
	tick(shipper, moment, 2*transcripts.FirstWait-time.Second)
	if tried := at.tried(); tried != 2 {
		t.Fatalf("got %d requests inside the doubled wait, want 2", tried)
	}
	at.refuse(false)
	tick(shipper, moment, transcripts.LongestWait)

	if sent := at.sent(); len(sent) != 1 || len(sent[0].Lines) != 3 {
		t.Fatalf("got %d shipments, want 1 of 3 lines", len(sent))
	}
	if behind := shipper.Behind(); behind != 0 {
		t.Errorf("got %d files behind, want 0", behind)
	}
}

func TestAServiceThatNeverAnswersIsRetriedWithNoLimit(t *testing.T) {
	at := newService(t)
	shipper, moment, _ := retrying(t, at, t.TempDir())
	path := transcript(t, 3)

	at.refuse(true)
	ship(t, shipper, path)
	tick(shipper, moment, 24*time.Hour)

	if behind := shipper.Behind(); behind != 1 {
		t.Errorf("got %d files behind after a day of refusals, want 1", behind)
	}
	// A day at the longest wait is 288 attempts, and the climb to it adds 6.
	if tried := at.tried(); tried < 288 {
		t.Errorf("got %d requests in a day, want at least 288", tried)
	}
}

func TestARestartedBastionShipsWhatWasBehindFromItsSavedOffset(t *testing.T) {
	at := newService(t)
	stateDir := t.TempDir()
	before, _, _ := retrying(t, at, stateDir)
	path := transcript(t, 2)

	if err := ship(t, before, path); err != nil {
		t.Fatal(err)
	}
	appendLines(t, path, 3, 4)
	at.refuse(true)
	ship(t, before, path)
	at.refuse(false)

	after, moment, _ := retrying(t, at, stateDir)
	after.Resume()
	if behind := after.Behind(); behind != 1 {
		t.Fatalf("a restart resumed %d files, want 1", behind)
	}
	tick(after, moment, transcripts.LongestWait)

	sent := at.sent()
	if len(sent) != 2 {
		t.Fatalf("got %d shipments, want 2", len(sent))
	}
	if sent[1].EntriesBefore != 2 || len(sent[1].Lines) != 2 || sent[1].Lines[0] != entry(3) {
		t.Errorf("got %d lines after %d entries, want entries 3 and 4 after 2", len(sent[1].Lines), sent[1].EntriesBefore)
	}
	if behind := after.Behind(); behind != 0 {
		t.Errorf("got %d files behind, want 0", behind)
	}
}

func TestAFileThatFailsPartwayResumesFromTheLastAcceptedBatch(t *testing.T) {
	at := newService(t)
	shipper, moment, _ := retrying(t, at, t.TempDir())
	path := transcript(t, transcripts.Batch+50)

	at.refuseOnce(2)
	if err := ship(t, shipper, path); err == nil {
		t.Fatal("got no error from a service that refused the second batch")
	}
	tick(shipper, moment, transcripts.LongestWait)

	received := map[string]bool{}
	total := 0
	for _, found := range at.sent() {
		for _, line := range found.Lines {
			received[line] = true
			total++
		}
	}
	if len(received) != transcripts.Batch+50 || total != transcripts.Batch+50 {
		t.Errorf("the service received %d lines, %d of them distinct, want %d once each",
			total, len(received), transcripts.Batch+50)
	}
	if sent := at.sent(); sent[len(sent)-1].EntriesBefore != transcripts.Batch {
		t.Errorf("the second batch came after %d entries, want %d", sent[len(sent)-1].EntriesBefore, transcripts.Batch)
	}
}

func TestTheStateFilesAreThePersonsAlone(t *testing.T) {
	at := newService(t)
	stateDir := t.TempDir()
	shipper, _, _ := retrying(t, at, stateDir)

	ship(t, shipper, transcript(t, 2))
	at.refuse(true)
	ship(t, shipper, transcript(t, 2))

	entries, err := os.ReadDir(stateDir)
	if err != nil {
		t.Fatal(err)
	}
	// One offset per transcript, and no temporary file left from a write.
	if len(entries) != 2 {
		t.Errorf("got %d files in the state directory, want 2", len(entries))
	}
	for _, found := range entries {
		info, err := found.Info()
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm() != 0o600 {
			t.Errorf("%s has mode %o, want 600", found.Name(), info.Mode().Perm())
		}
	}
}
