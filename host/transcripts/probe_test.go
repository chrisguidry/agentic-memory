package transcripts_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/chrisguidry/agentic-memory/host/transcripts"
)

// probed writes a transcript of two entries and marks it as a probe's.
func probed(t *testing.T, shipper *transcripts.Shipper) string {
	t.Helper()
	path := transcript(t, 2)
	if err := shipper.MarkProbe(path); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestAProbeTranscriptIsNeverShippedByALaterEvent(t *testing.T) {
	at := newService(t)
	shipper := newShipper(t, at)
	path := probed(t, shipper)
	appendLines(t, path, 3, 4)

	if err := ship(t, shipper, path); err != nil {
		t.Fatal(err)
	}

	if sent := at.sent(); len(sent) != 0 {
		t.Errorf("got %d shipments of a probe's transcript, want 0", len(sent))
	}
	if found := shipper.Recorded(path); found != transcripts.ProbeRecord {
		t.Errorf("the record is %q, want %q", found, transcripts.ProbeRecord)
	}
}

// A subagent's transcript is in a directory named after its session's
// transcript, and a backfill finds it there. The mark on the session covers it.
func TestASubagentOfAProbeIsNeverShipped(t *testing.T) {
	at := newService(t)
	shipper := newShipper(t, at)
	path := probed(t, shipper)
	subagent := filepath.Join(strings.TrimSuffix(path, ".jsonl"), "subagents", "agent-1.jsonl")
	if err := os.MkdirAll(filepath.Dir(subagent), 0o700); err != nil {
		t.Fatal(err)
	}
	write(t, subagent, 2)

	if err := ship(t, shipper, subagent); err != nil {
		t.Fatal(err)
	}

	if sent := at.sent(); len(sent) != 0 {
		t.Errorf("got %d shipments of a probe's subagent, want 0", len(sent))
	}
}

// A transcript that was behind when a probe marked it is dropped from the
// retries, and the mark stays.
func TestAProbeTranscriptThatWasBehindIsNeverRetried(t *testing.T) {
	at := newService(t)
	shipper, moment, _ := retrying(t, at, t.TempDir())
	path := transcript(t, 2)
	at.refuse(true)
	ship(t, shipper, path)
	at.refuse(false)

	if err := shipper.MarkProbe(path); err != nil {
		t.Fatal(err)
	}
	tick(shipper, moment, transcripts.LongestWait)

	if sent := at.sent(); len(sent) != 0 {
		t.Errorf("got %d shipments of a probe's transcript, want 0", len(sent))
	}
	if found := shipper.Recorded(path); found != transcripts.ProbeRecord {
		t.Errorf("the record is %q, want %q", found, transcripts.ProbeRecord)
	}
}

func TestARestartedBastionNeverShipsAProbeTranscriptThatWasBehind(t *testing.T) {
	at := newService(t)
	stateDir := t.TempDir()
	before, _, _ := retrying(t, at, stateDir)
	path := transcript(t, 2)
	at.refuse(true)
	ship(t, before, path)
	at.refuse(false)
	if err := before.MarkProbe(path); err != nil {
		t.Fatal(err)
	}

	after, moment, _ := retrying(t, at, stateDir)
	after.Resume()
	tick(after, moment, transcripts.LongestWait)

	if sent := at.sent(); len(sent) != 0 {
		t.Errorf("got %d shipments of a probe's transcript, want 0", len(sent))
	}
}

func TestTheRecordOfATranscriptSaysWhetherItIsAProbes(t *testing.T) {
	at := newService(t)
	shipper := newShipper(t, at)
	live := transcript(t, 2)
	if err := ship(t, shipper, live); err != nil {
		t.Fatal(err)
	}
	probe := probed(t, shipper)

	for _, test := range []struct {
		path string
		want transcripts.Record
	}{
		{live, transcripts.LiveRecord},
		{probe, transcripts.ProbeRecord},
		{transcript(t, 2), transcripts.NoRecord},
	} {
		if found := shipper.Recorded(test.path); found != test.want {
			t.Errorf("the record of %s is %q, want %q", test.path, found, test.want)
		}
	}
}
