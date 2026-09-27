package transcripts

import (
	"errors"
	"io/fs"
	"syscall"
)

// cause is why a file did not ship. Each cause has its own handling, because a
// retry fixes only some of them.
type cause int

const (
	// serviceFailed is a failure that waiting can fix: the service answered
	// with something other than a 2xx, or the network, DNS, or TLS failed. A
	// local error that is not about the path, such as too many open files, is
	// here too.
	serviceFailed cause = iota
	// notWrittenYet is a file that does not exist and that the bastion has
	// never shipped from. Claude Code names the transcript in a session's first
	// event, and can fire that event before it writes the file.
	notWrittenYet
	// gone is a file that another read will not fix: a file the bastion shipped
	// from before and that is now deleted, or a path that is not a file this
	// user can read.
	gone
)

// unreadable is an error from reading the transcript, as opposed to sending
// what was read. Only an error of this type is about the file, so a network
// error that wraps "no such file" is never taken for a missing transcript.
type unreadable struct {
	err error
	// shipped is whether the bastion shipped from this file before. A file it
	// shipped from existed, so its absence now is a deletion.
	shipped bool
}

func (u unreadable) Error() string { return u.err.Error() }
func (u unreadable) Unwrap() error { return u.err }

// errNotAFile is a transcript path that names a directory, a device, or a pipe.
// None of them is a transcript, and none becomes one on a retry.
var errNotAFile = errors.New("not a regular file")

func causeOf(err error) cause {
	var local unreadable
	if !errors.As(err, &local) {
		return serviceFailed
	}
	switch {
	case errors.Is(err, fs.ErrNotExist) && !local.shipped:
		return notWrittenYet
	case errors.Is(err, fs.ErrNotExist),
		errors.Is(err, fs.ErrPermission),
		errors.Is(err, syscall.ENOTDIR),
		errors.Is(err, syscall.ELOOP),
		errors.Is(err, syscall.ENAMETOOLONG),
		errors.Is(err, errNotAFile):
		return gone
	}
	return serviceFailed
}
