// Package backfill loads the sessions a harness already wrote into the
// service. It asks the bastion to ship one file at a time, so the machine's
// address and authorization stay in the bastion and the formats stay on the
// server.
package backfill

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/chrisguidry/agentic-memory/host/socket"
)

// Deadline bounds one file's shipment. A session file of a few hundred
// megabytes goes to the service in many batches, and every one of them has to
// finish inside this.
const Deadline = 30 * time.Minute

// Usage is the backfill's help text.
const Usage = `agentic-memory backfill --harness <claude-code|codex|pi>

Load the sessions a harness already wrote into the service. The bastion reads
each file and ships its lines, so this command holds no address and no
authorization.

  --harness   claude-code, codex, or pi
  --from      a session directory, or a copy of one from another machine.
              Defaults to where the harness keeps its own sessions.
  --machine   the machine the transcripts came from. Defaults to this host's
              name. Name it when the transcripts came from somewhere else, or
              every record will say the wrong machine, and a record that names
              the wrong machine is worse than one that names none, because it
              looks right.
  --dry-run   print the files this would ship, and ship nothing.
  --socket    the bastion's socket. Defaults to AGENTIC_MEMORY_SOCKET, or the
              socket under the runtime directory.
`

// counts is what the bastion reports for one file, summed over every batch the
// file took.
type counts struct {
	Received int `json:"received"`
	Inserted int `json:"inserted"`
	Repeated int `json:"repeated"`
}

func (c *counts) add(found counts) {
	c.Received += found.Received
	c.Inserted += found.Inserted
	c.Repeated += found.Repeated
}

// options is what one run was asked for.
type options struct {
	harness string
	source  string
	machine string
	dryRun  bool
	socket  string
}

// Run backfills one harness and prints what the service counted.
func Run(arguments []string, out io.Writer) error {
	chosen, err := parse(arguments, out)
	if err != nil {
		return err
	}

	files, err := Discover(chosen.source)
	if err != nil {
		return err
	}
	if len(files) == 0 {
		return fmt.Errorf("no %s sessions under %s", chosen.harness, chosen.source)
	}

	if chosen.dryRun {
		for _, path := range files {
			fmt.Fprintln(out, path)
		}
		fmt.Fprintf(out, "%d %s session files under %s, and nothing sent\n",
			len(files), chosen.harness, chosen.source)
		return nil
	}

	fmt.Fprintf(out, "%d %s session files under %s\n", len(files), chosen.harness, chosen.source)
	fmt.Fprintf(out, "shipping them as %s through %s\n", chosen.machine, chosen.socket)

	client := &http.Client{Transport: over(chosen.socket)}
	started := time.Now()
	var total counts
	for number, path := range files {
		found, err := ship(client, chosen, path)
		if err != nil {
			return err
		}
		total.add(found)
		// One line per file, written as the file finishes, because a backfill
		// of a two-gigabyte source runs for long enough that silence reads as
		// a hang.
		fmt.Fprintf(out, "  %d/%d %s: %d new, %d already held\n",
			number+1, len(files), path, found.Inserted, found.Repeated)
	}

	fmt.Fprintf(out, "done in %.0fs: %d records, %d new, %d already held\n",
		time.Since(started).Seconds(), total.Received, total.Inserted, total.Repeated)
	return nil
}

func parse(arguments []string, out io.Writer) (options, error) {
	var chosen options
	flags := flag.NewFlagSet("backfill", flag.ContinueOnError)
	flags.SetOutput(out)
	flags.Usage = func() { fmt.Fprint(out, Usage) }
	flags.StringVar(&chosen.harness, "harness", "", "the harness to read: "+strings.Join(Harnesses(), ", "))
	flags.StringVar(&chosen.source, "from", "", "a session directory, or a copy of one from another machine")
	flags.StringVar(&chosen.machine, "machine", "", "the machine the transcripts came from")
	flags.BoolVar(&chosen.dryRun, "dry-run", false, "print the files this would ship, and ship nothing")
	flags.StringVar(&chosen.socket, "socket", socket.Path(), "the bastion's socket")
	if err := flags.Parse(arguments); err != nil {
		return chosen, err
	}

	if !known(chosen.harness) {
		return chosen, fmt.Errorf("--harness is one of %s", strings.Join(Harnesses(), ", "))
	}
	if chosen.source == "" {
		chosen.source = Source(chosen.harness, home())
	}
	if chosen.machine == "" {
		chosen.machine = machine()
	}
	return chosen, nil
}

func known(harness string) bool {
	for _, named := range Harnesses() {
		if named == harness {
			return true
		}
	}
	return false
}

func machine() string {
	if named := os.Getenv("AGENTIC_MEMORY_MACHINE"); named != "" {
		return named
	}
	name, err := os.Hostname()
	if err != nil {
		return "unknown"
	}
	return name
}

// ship asks the bastion for one file. The working directory goes unsent,
// because the bastion reads it from the file, and the file is the only record
// of where the session ran.
func ship(client *http.Client, chosen options, path string) (counts, error) {
	var found counts
	body, err := json.Marshal(map[string]string{
		"harness": chosen.harness,
		"path":    path,
		"machine": chosen.machine,
	})
	if err != nil {
		return found, err
	}

	ctx, cancel := context.WithTimeout(context.Background(), Deadline)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodPost,
		"http://bastion/transcripts/ship", bytes.NewReader(body))
	if err != nil {
		return found, err
	}
	request.Header.Set("Content-Type", "application/json")

	response, err := client.Do(request)
	if err != nil {
		// Unlike the hook, a backfill that cannot reach the bastion says so
		// and stops, because the person is watching it and nothing else will
		// send these files.
		return found, fmt.Errorf("could not reach the bastion on %s: %w", chosen.socket, err)
	}
	defer response.Body.Close()
	answer, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if err != nil {
		return found, err
	}
	if response.StatusCode < 200 || response.StatusCode > 299 {
		return found, fmt.Errorf("%s: the bastion answered %s: %s",
			path, response.Status, strings.TrimSpace(string(answer)))
	}
	if err := json.Unmarshal(answer, &found); err != nil {
		return found, fmt.Errorf("%s: %w", path, err)
	}
	return found, nil
}

// over dials the bastion's socket for every request. The host in the URL is
// never resolved, because the socket is the address.
func over(path string) http.RoundTripper {
	return &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			var dialer net.Dialer
			return dialer.DialContext(ctx, "unix", path)
		},
	}
}
