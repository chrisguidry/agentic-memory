package bastion_test

import (
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/chrisguidry/agentic-memory/host/bastion"
)

func TestTheBastionSaysHowManyFilesAreBehind(t *testing.T) {
	service := httptest.NewServer(http.NotFoundHandler())
	defer service.Close()
	routes, _, err := bastion.Routes(bastion.Config{
		Service:  service.URL,
		StateDir: t.TempDir(),
		Machine:  "laptop",
	}, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	socket := httptest.NewServer(routes)
	defer socket.Close()

	// A transcript Claude Code has not written yet waits out its grace period,
	// and it is behind while it waits.
	unwritten := filepath.Join(t.TempDir(), "session.jsonl")
	shipped, err := http.Post(socket.URL+"/transcripts/ship", "application/json",
		strings.NewReader(`{"harness": "claude-code", "path": "`+unwritten+`"}`))
	if err != nil {
		t.Fatal(err)
	}
	shipped.Body.Close()

	response, err := http.Get(socket.URL + "/transcripts/behind")
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	var answer struct {
		Behind int `json:"behind"`
	}
	if err := json.NewDecoder(response.Body).Decode(&answer); err != nil {
		t.Fatal(err)
	}
	if answer.Behind != 1 {
		t.Errorf("got %d files behind, want 1", answer.Behind)
	}
}
