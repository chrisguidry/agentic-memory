package bastion

import (
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"time"
)

// transport is the one connection pool the bastion uses for everything it
// sends to the service: recall, transcripts, and every proxied request. The
// idle timeout is long, so the connection and its TLS session stay warm
// between one turn and the next.
func transport() *http.Transport {
	return &http.Transport{
		MaxIdleConnsPerHost: 4,
		IdleConnTimeout:     10 * time.Minute,
		ForceAttemptHTTP2:   true,
	}
}

// proxy passes a request on to the service with the authorization header
// added. The path and the query go through unchanged, so anything already
// written against the service works against the socket.
func proxy(service, authorization string, rounds http.RoundTripper, logger *log.Logger) (*httputil.ReverseProxy, error) {
	base, err := url.Parse(service)
	if err != nil {
		return nil, err
	}
	return &httputil.ReverseProxy{
		Transport: rounds,
		// Without this, the proxy writes its own failures through the standard
		// logger, with a timestamp the journal adds again.
		ErrorLog: logger,
		Rewrite: func(request *httputil.ProxyRequest) {
			request.SetURL(base)
			request.Out.Host = base.Host
			if authorization != "" {
				request.Out.Header.Set("Authorization", authorization)
			}
		},
	}, nil
}
