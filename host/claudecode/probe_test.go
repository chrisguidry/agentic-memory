package claudecode_test

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"testing"
	"time"

	"github.com/chrisguidry/agentic-memory/host/recall"
	"github.com/chrisguidry/agentic-memory/host/transcripts"
)

// Every probe event marks the transcript, so no later event ships it, and
// ships nothing itself.
func TestAProbeIsNeverShipped(t *testing.T) {
	for _, event := range []string{"UserPromptSubmit", "Stop", "SubagentStop", "SessionEnd"} {
		t.Run(event, func(t *testing.T) {
			at := newService(t)
			bastion, shipper := newHooks(t, at, nil)
			path := transcript(t)

			probe(t, bastion, map[string]any{
				"session_id": session, "hook_event_name": event,
				"cwd": "/work/widget", "prompt": "how do we ship this?",
				"transcript_path": path,
			})
			// A live event ships after its response is written, so the server
			// is closed before the counts are read: Close waits for every
			// handler to return.
			bastion.Close()

			if shipped := at.shipped(); shipped != 0 {
				t.Errorf("got %d shipments, want 0", shipped)
			}
			if found := shipper.Recorded(path); found != transcripts.ProbeRecord {
				t.Errorf("the record is %q, want %q", found, transcripts.ProbeRecord)
			}
		})
	}
}

// `claude --continue` and `claude --resume` without the variable send a live
// event on a probe's transcript.
func TestALiveEventAfterAProbeShipsNothingOfIt(t *testing.T) {
	at := newService(t)
	bastion, _ := newHooks(t, at, nil)
	path := transcript(t)
	event := func(name string) map[string]any {
		return map[string]any{
			"session_id": session, "hook_event_name": name,
			"cwd": "/work/widget", "prompt": "how do we ship this?",
			"transcript_path": path,
		}
	}

	probe(t, bastion, event("UserPromptSubmit"))
	probe(t, bastion, event("Stop"))
	probe(t, bastion, event("SessionEnd"))
	fire(t, bastion, event("UserPromptSubmit"))
	bastion.Close()

	if shipped := at.shipped(); shipped != 0 {
		t.Errorf("got %d shipments, want 0", shipped)
	}
}

func TestAProbePrintsWhatALiveTurnPrints(t *testing.T) {
	at := newService(t, recall.Statement{
		ID: 7, Statement: "Tests come before code here.",
		Kind: "preference", ScopeKey: "example.test/acme/widget",
		SaidAt: now.AddDate(0, 0, -3).Format(time.RFC3339), Actor: "human",
	})
	bastion, _ := newHooks(t, at, nil)
	prompt := map[string]any{
		"session_id": session, "hook_event_name": "UserPromptSubmit",
		"cwd": "/work/widget", "prompt": "how do we ship this?",
	}

	live := fire(t, bastion, prompt)
	if live == "" {
		t.Fatal("the live turn printed nothing")
	}
	if probed := probe(t, bastion, prompt); probed != live {
		t.Errorf("the probe printed\n%q\nand the live turn printed\n%q", probed, live)
	}
}

// A probe session is sent what the bastion handed it before, because the
// service records nothing for a probe. Each `claude -p` process ends its
// session, and `claude -p --resume` goes on with it, so SessionEnd forgets
// nothing.
func TestAProbeSessionSendsWhatItWasHanded(t *testing.T) {
	at := newService(t,
		recall.Statement{ID: 9, Statement: "Commit messages say why."},
		recall.Statement{ID: 7, Statement: "Tests come before code here."},
	)
	bastion, _ := newHooks(t, at, nil)
	event := func(session, name string) map[string]any {
		return map[string]any{
			"session_id": session, "hook_event_name": name,
			"cwd": "/work/widget", "prompt": "how do we ship this?",
		}
	}

	probe(t, bastion, event("probe-a", "UserPromptSubmit"))
	probe(t, bastion, event("probe-a", "UserPromptSubmit"))
	probe(t, bastion, event("probe-b", "UserPromptSubmit"))
	fire(t, bastion, event("live-c", "UserPromptSubmit"))
	probe(t, bastion, event("probe-a", "SessionEnd"))
	probe(t, bastion, event("probe-a", "UserPromptSubmit"))

	if !reflect.DeepEqual(at.probes, []probed{
		{SessionID: "probe-a"},
		{SessionID: "probe-a", Seen: []int64{7, 9}},
		{SessionID: "probe-b"},
		{SessionID: "probe-a", Seen: []int64{7, 9}},
	}) {
		t.Errorf("got the probes %+v", at.probes)
	}
	if len(at.asks) != 1 || at.asks[0].SessionID != "live-c" {
		t.Errorf("got the live asks %+v, want one for live-c", at.asks)
	}
}

// preflight reads what the bastion says about probes, for one transcript or
// for none.
func preflight(t *testing.T, bastion *httptest.Server, path string) map[string]any {
	t.Helper()
	client := &http.Client{Timeout: 5 * time.Second}
	response, err := client.Get(bastion.URL + "/claude-code/probes?transcript=" + url.QueryEscape(path))
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("got %s, want 200", response.Status)
	}
	var found map[string]any
	if err := json.NewDecoder(response.Body).Decode(&found); err != nil {
		t.Fatal(err)
	}
	return found
}

func TestThePreflightSaysWhatTheBastionRecordsForATranscript(t *testing.T) {
	at := newService(t)
	bastion, shipper := newHooks(t, at, nil)
	live, probed := transcript(t), transcript(t)
	if _, err := shipper.Ship(t.Context(), transcripts.Request{
		Harness: "claude-code", Path: live, Machine: "laptop",
	}); err != nil {
		t.Fatal(err)
	}
	// The mark is written before the probe's answer, so it is there at once.
	probe(t, bastion, map[string]any{
		"session_id": session, "hook_event_name": "Stop", "cwd": "/work/widget", "transcript_path": probed,
	})

	for _, test := range []struct {
		path   string
		record string
	}{
		{"", ""},
		{live, "live"},
		{probed, "probe"},
		{transcript(t), "none"},
	} {
		found := preflight(t, bastion, test.path)
		want := map[string]any{"probes": true}
		if test.record != "" {
			want["record"] = test.record
		}
		if !reflect.DeepEqual(found, want) {
			t.Errorf("for %q the preflight answered %v, want %v", test.path, found, want)
		}
	}
}
