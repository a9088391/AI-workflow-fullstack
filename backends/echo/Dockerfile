FROM golang:1.26.8-alpine3.24@sha256:ce864e7223ac17b1775e6fd0b4c0db580c2eb50e7953a427916379e4b92a1628 AS build
WORKDIR /src
COPY backends/echo/go.mod backends/echo/go.sum ./
RUN GOTOOLCHAIN=local go mod download
COPY backends/echo/*.go ./
RUN GOTOOLCHAIN=local CGO_ENABLED=0 go build -trimpath -o /echo .

FROM golang:1.26.8-alpine3.24@sha256:ce864e7223ac17b1775e6fd0b4c0db580c2eb50e7953a427916379e4b92a1628
COPY --from=build /echo /echo
EXPOSE 8000
CMD ["/echo"]
