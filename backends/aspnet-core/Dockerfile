FROM mcr.microsoft.com/dotnet/sdk:10.0.301-alpine3.24@sha256:fc785a84b314fce6b7adb29ecc18542d7416f2e05c03ec1ebdb222eadaeafa50 AS build
WORKDIR /src
COPY backends/aspnet-core/AspNetCore.csproj backends/aspnet-core/packages.lock.json ./
RUN dotnet restore AspNetCore.csproj --locked-mode
COPY backends/aspnet-core/*.cs ./
RUN dotnet publish AspNetCore.csproj -c Release --no-restore -o /out

FROM mcr.microsoft.com/dotnet/aspnet:10.0.9-alpine3.24@sha256:ab9cd53240fe765e9ea01cb3f043ee92607072440fc5b252ca4d64b48c7c17fe
RUN apk add --no-cache tzdata=2026f-r0
WORKDIR /app
COPY --from=build /out ./
ENV ASPNETCORE_URLS=http://0.0.0.0:8080
EXPOSE 8080
ENTRYPOINT ["dotnet", "AspNetCore.dll"]
