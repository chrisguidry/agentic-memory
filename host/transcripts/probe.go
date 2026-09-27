package transcripts

import (
	"os"
	"path/filepath"
	"strings"
)

// Record is what the bastion holds for one transcript.
type Record string

const (
	// NoRecord is a transcript the bastion has never shipped or marked.
	NoRecord Record = "none"
	// LiveRecord is a transcript with an offset record and no probe mark. A
	// later event on it ships what it gained.
	LiveRecord Record = "live"
	// ProbeRecord is a transcript marked as a probe session's, which Ship never
	// sends.
	ProbeRecord Record = "probe"
)

// MarkProbe records that the transcript at path belongs to a probe session.
//
// Ship never sends a marked transcript, whoever asks: the live hook, a retry,
// a restarted bastion, or a backfill. Without the mark, a later live event on
// the same path, such as `claude --continue`, finds no offset and ships the
// whole file from byte zero, probe prompts included. The mark is in the offset
// record on disk, so it outlasts a restart, and nothing removes it.
func (s *Shipper) MarkProbe(path string) error {
	lock := s.lock(path)
	lock.Lock()
	defer lock.Unlock()

	name := offsetFile(s.StateDir, path)
	record := readOffset(name)
	if record.Probe {
		return nil
	}
	record.Path = path
	record.Probe = true
	return writeOffset(name, record)
}

// Recorded says what the bastion holds for the transcript at path.
func (s *Shipper) Recorded(path string) Record {
	if s.probe(path) {
		return ProbeRecord
	}
	if _, err := os.Stat(offsetFile(s.StateDir, path)); err != nil {
		return NoRecord
	}
	return LiveRecord
}

// probe reports whether the transcript at path, or the session transcript it
// belongs to, is marked as a probe's.
func (s *Shipper) probe(path string) bool {
	if readOffset(offsetFile(s.StateDir, path)).Probe {
		return true
	}
	session, found := sessionOf(path)
	return found && readOffset(offsetFile(s.StateDir, session)).Probe
}

// sessionOf returns the session transcript a subagent's transcript belongs to.
// Claude Code writes a subagent to `<session>/subagents/<agent>.jsonl` beside
// `<session>.jsonl`, and names only the session's transcript in the hook
// payload. A probe marks that one, and a backfill finds the subagent's file
// on its own.
func sessionOf(path string) (string, bool) {
	directory := filepath.Dir(path)
	if filepath.Base(directory) != "subagents" || !strings.HasSuffix(path, ".jsonl") {
		return "", false
	}
	return filepath.Dir(directory) + ".jsonl", true
}
