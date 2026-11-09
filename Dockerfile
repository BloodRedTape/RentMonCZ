FROM golang:1.27-alpine AS build
WORKDIR /src
COPY service/go.mod service/go.sum ./
RUN go mod download
COPY service/cmd ./cmd
COPY service/internal ./internal
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/rentmont ./cmd/rentmont \
    && mkdir -p /out/data

FROM gcr.io/distroless/static-debian12
WORKDIR /app
ENV DATA_DIR=/app/data
COPY --from=build /out/rentmont /usr/local/bin/rentmont
COPY --from=build /out/data /app/data
ENTRYPOINT ["/usr/local/bin/rentmont"]
