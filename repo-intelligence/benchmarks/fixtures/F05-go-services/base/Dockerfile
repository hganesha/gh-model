FROM golang:1.24 AS build
WORKDIR /src
COPY . .
RUN go build -o /out/gateway ./cmd/gateway
FROM gcr.io/distroless/base
COPY --from=build /out/gateway /gateway
ENTRYPOINT ["/gateway"]
