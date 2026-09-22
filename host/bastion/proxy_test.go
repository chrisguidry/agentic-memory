package bastion_test

import (
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"

	"github.com/chrisguidry/agentic-memory/host/bastion"
)

func TestEveryOtherPathGoesToTheServiceWithTheAuthorization(t *testing.T) {
	var asked *url.URL
	var authorization string
	service := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		asked = r.URL
		authorization = r.Header.Get("Authorization")
		io.WriteString(w, `{"memories": []}`)
	}))
	defer service.Close()

	routes, _, err := bastion.Routes(bastion.Config{
		Service:       service.URL,
		Authorization: "Basic bm90LWEtcmVhbC1zZWNyZXQ=",
		StateDir:      t.TempDir(),
		Machine:       "laptop",
		Limit:         10,
	}, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	socket := httptest.NewServer(routes)
	defer socket.Close()

	response, err := http.Get(socket.URL + "/memories?scope=example.test%2Facme%2Fwidget&limit=3")
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}

	if response.StatusCode != http.StatusOK {
		t.Fatalf("got %s, want 200", response.Status)
	}
	if string(body) != `{"memories": []}` {
		t.Errorf("got %q from the service", body)
	}
	if asked.Path != "/memories" {
		t.Errorf("got the path %q, want %q", asked.Path, "/memories")
	}
	if asked.Query().Get("scope") != "example.test/acme/widget" || asked.Query().Get("limit") != "3" {
		t.Errorf("got the query %q", asked.RawQuery)
	}
	if authorization != "Basic bm90LWEtcmVhbC1zZWNyZXQ=" {
		t.Errorf("got the authorization %q", authorization)
	}
}

func TestTheBastionNeedsAService(t *testing.T) {
	err := bastion.Run(t.Context(), bastion.Config{StateDir: t.TempDir()}, log.New(io.Discard, "", 0))
	if err == nil {
		t.Fatal("got no error from a bastion with no service")
	}
}
