# syntax=docker/dockerfile:1.7
FROM gradle:9.6.1-jdk21 AS build

WORKDIR /workspace
COPY . .
RUN --mount=type=cache,target=/home/gradle/.gradle \
	--mount=type=cache,target=/workspace/.gradle \
	gradle --no-daemon build

FROM scratch AS artifacts
COPY --from=build /workspace/build/libs/mothers-ledger-0.1.0.jar /artifacts/mothers-ledger-neoforge.jar