package runlog

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"

	"github.com/dvygo/premarket/v4g/internal/paths"
)

// Setup tees stderr to console and bin/LOGS/<binary>_<dateDir>_<timestamp>.log.
// Call the returned cleanup from defer (e.g. defer cleanup()).
func Setup(binaryName, dateDir string) (cleanup func(), logPath string, err error) {
	if err := paths.EnsureBinDirs(); err != nil {
		return func() {}, "", err
	}
	ts := time.Now().Format("20060102_150405")
	logPath = filepath.Join(paths.LogsDir(), fmt.Sprintf("%s_%s_%s.log", binaryName, dateDir, ts))
	f, err := os.Create(logPath)
	if err != nil {
		return func() {}, "", err
	}

	orig := os.Stderr
	r, w, err := os.Pipe()
	if err != nil {
		f.Close()
		return func() {}, "", err
	}
	os.Stderr = w

	done := make(chan struct{})
	go func() {
		defer close(done)
		buf := make([]byte, 32*1024)
		for {
			n, readErr := r.Read(buf)
			if n > 0 {
				chunk := buf[:n]
				_, _ = orig.Write(chunk)
				_, _ = f.Write(chunk)
			}
			if readErr != nil {
				if readErr != io.EOF {
					_, _ = fmt.Fprintf(orig, "runlog: read stderr pipe: %v\n", readErr)
				}
				break
			}
		}
	}()

	cleanup = func() {
		w.Close()
		<-done
		os.Stderr = orig
		f.Close()
	}
	return cleanup, logPath, nil
}
