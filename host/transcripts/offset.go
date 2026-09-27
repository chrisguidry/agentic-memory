package transcripts

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
)

// head is how much of the file's head the fingerprint covers. A rewrite that
// keeps the first four kilobytes byte for byte and is no shorter than the
// offset is not caught, and the service drops what it re-sends.
const head = 4096

// offset is how far the bastion has read one transcript, and what it recorded
// about the file from the part it has already sent.
//
// Cwd and Version are the first the bastion ever saw for this file. The
// service needs both, and a chunk from the middle of a file may carry neither.
type offset struct {
	Path    string `json:"path"`
	Offset  int64  `json:"offset"`
	Entries int    `json:"entries"`
	Head    string `json:"head"`
	Cwd     string `json:"cwd"`
	Version string `json:"version"`
	// Behind is set while the file has lines the service has not taken.
	Behind *behind `json:"behind,omitempty"`
	// Probe is set on a probe session's transcript, which is never shipped.
	Probe bool `json:"probe,omitempty"`
}

// offsetFile names the state file for a transcript. The name is a hash of the
// path, so a transcript anywhere on the machine has one file with a name a
// directory accepts.
func offsetFile(stateDir, path string) string {
	sum := sha256.Sum256([]byte(path))
	return filepath.Join(stateDir, hex.EncodeToString(sum[:])[:16]+".json")
}

// readOffset returns what the bastion last recorded for a transcript. A file
// that is missing or unreadable is a transcript the bastion has never seen,
// and re-sending one costs bandwidth and never a duplicate.
func readOffset(name string) offset {
	var found offset
	raw, err := os.ReadFile(name)
	if err != nil {
		return offset{}
	}
	if err := json.Unmarshal(raw, &found); err != nil {
		return offset{}
	}
	return found
}

// writeOffset replaces a record whole. It writes a temporary file and renames it
// over the record, so a bastion stopped in the middle of a write leaves the old
// record or the new one. A half-written record reads as a file never seen, and
// the whole transcript is sent again.
func writeOffset(name string, found offset) error {
	if err := os.MkdirAll(filepath.Dir(name), 0o700); err != nil {
		return err
	}
	raw, err := json.Marshal(found)
	if err != nil {
		return err
	}
	// CreateTemp makes the file with mode 0600, and the rename keeps the mode.
	// The name does not end in .json, so Resume never reads a leftover one.
	temporary, err := os.CreateTemp(filepath.Dir(name), ".offset-*")
	if err != nil {
		return err
	}
	defer os.Remove(temporary.Name())
	if _, err := temporary.Write(raw); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Sync(); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	return os.Rename(temporary.Name(), name)
}

// fingerprint hashes the head of the file, over bytes the bastion has already
// shipped.
//
// Only shipped bytes can be compared, because the file grows between events
// and a hash over more than the offset would change every time.
func fingerprint(file io.ReadSeeker, upTo int64) (string, error) {
	if _, err := file.Seek(0, io.SeekStart); err != nil {
		return "", err
	}
	if upTo > head {
		upTo = head
	}
	raw, err := io.ReadAll(io.LimitReader(file, upTo))
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}
