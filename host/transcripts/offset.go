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

func writeOffset(name string, found offset) error {
	if err := os.MkdirAll(filepath.Dir(name), 0o700); err != nil {
		return err
	}
	raw, err := json.Marshal(found)
	if err != nil {
		return err
	}
	return os.WriteFile(name, raw, 0o600)
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
