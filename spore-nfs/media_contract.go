package main

import (
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"regexp"
	"strconv"
)

// Discovery metadata is not playable media. Do not return a tiny MKV in place
// of an unavailable movie, even when a client elects Direct Play itself.
const minimumMediaSize int64 = 1 << 20

var errMediaUnavailable = fmt.Errorf("spore: real media unavailable: %w", os.ErrPermission)

func playableMediaSize(cached bool, size, headerSize int64) (int64, error) {
	if !cached || size < minimumMediaSize {
		return 0, errMediaUnavailable
	}
	// A previous release's fast-start header must not describe a new object.
	if headerSize != 0 && headerSize != size {
		return 0, fmt.Errorf("spore: media/header size mismatch: %w", errMediaUnavailable)
	}
	return size, nil
}

var mediaContentRange = regexp.MustCompile(`^bytes ([0-9]+)-([0-9]+)/([0-9]+)$`)

// Consume actual media ranges, never an error page, a frame-empty placeholder,
// or offset-zero data mislabeled as a successful seek. All callers share this
// check, including resolved CDN URLs. No materialization is performed here.
func readMediaResponse(resp *http.Response, offset, length int64) ([]byte, error) {
	if offset < 0 || length <= 0 {
		return nil, errors.New("spore: invalid media range")
	}
	var total, expected int64
	switch resp.StatusCode {
	case http.StatusPartialContent:
		parts := mediaContentRange.FindStringSubmatch(resp.Header.Get("Content-Range"))
		if parts == nil {
			return nil, errors.New("spore: missing or invalid media Content-Range")
		}
		values := make([]int64, 3)
		for i := range values {
			n, err := strconv.ParseInt(parts[i+1], 10, 64)
			if err != nil {
				return nil, errors.New("spore: overflowing media Content-Range")
			}
			values[i] = n
		}
		start, end := values[0], values[1]
		total = values[2]
		if start != offset || end < start || end >= total {
			return nil, errors.New("spore: media range does not match requested offset")
		}
		expected = end - start + 1
		want := length
		if total-offset < want {
			want = total - offset
		}
		if expected != want || (resp.ContentLength >= 0 && resp.ContentLength != expected) {
			return nil, errors.New("spore: incomplete or inconsistent media range")
		}
	case http.StatusOK:
		// A server may ignore a Range header only for the first read. Never
		// return the beginning of the movie as a successful nonzero seek.
		if offset != 0 {
			return nil, errors.New("spore: upstream ignored nonzero media range")
		}
		total = resp.ContentLength
		expected = length
		if total < expected {
			expected = total
		}
	default:
		return nil, fmt.Errorf("spore: media response status %d", resp.StatusCode)
	}
	if total < minimumMediaSize || expected <= 0 {
		return nil, errMediaUnavailable
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, expected))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) != expected {
		return nil, io.ErrUnexpectedEOF
	}
	return data, nil
}
