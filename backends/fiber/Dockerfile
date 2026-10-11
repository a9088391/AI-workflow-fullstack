FROM golang:1.26.8-alpine3.24@sha256:ce864e7223ac17b1775e6fd0b4c0db580c2eb50e7953a427916379e4b92a1628 AS build
WORKDIR /src
COPY backends/fiber/go.mod backends/fiber/go.sum ./
RUN GOTOOLCHAIN=local go mod download
COPY backends/fiber/*.go ./
RUN GOTOOLCHAIN=local CGO_ENABLED=0 go build -trimpath -o /fiber .

FROM golang:1.26.8-alpine3.24@sha256:ce864e7223ac17b1775e6fd0b4c0db580c2eb50e7953a427916379e4b92a1628
COPY --from=build /fiber /fiber
EXPOSE 8000
CMD ["/fiber"]
