# protoc and protoc-gen-grpc-java are glibc executables, so code generation
# stays on the Maven/Temurin image. Both defaults use the same immutable Java
# 21 image: this keeps local builds deterministic when the registry is
# unavailable and prevents a Java 21 artifact from running on an older JRE.
# Production may still override JAVA_RUNTIME_IMAGE with a slimmer Java 21 JRE.
ARG JAVA_BUILD_IMAGE=maven:3.9.12-eclipse-temurin-21@sha256:c3c9d3ac4ce8431a3995c0318b8d390f448e693dd4fabc16e9b68d2e1f3d7b46
ARG JAVA_RUNTIME_IMAGE=maven:3.9.12-eclipse-temurin-21@sha256:c3c9d3ac4ce8431a3995c0318b8d390f448e693dd4fabc16e9b68d2e1f3d7b46

FROM ${JAVA_BUILD_IMAGE} AS build

WORKDIR /workspace
RUN java -XshowSettings:properties -version 2>&1 \
    | grep -Eq 'java\.specification\.version = 21$'
COPY .mvn .mvn
COPY mvnw pom.xml ./
COPY --from=contracts /agent-runtime/v1 /contracts/agent-runtime/v1
RUN --mount=type=cache,id=ratsnest-maven,target=/root/.m2,sharing=locked \
    ./mvnw -B -ntp dependency:go-offline
COPY src src
RUN --mount=type=cache,id=ratsnest-maven,target=/root/.m2,sharing=locked \
    ./mvnw -B -ntp verify

FROM ${JAVA_RUNTIME_IMAGE}

WORKDIR /app
RUN java -XshowSettings:properties -version 2>&1 \
    | grep -Eq 'java\.specification\.version = 21$'
COPY --from=build /workspace/target/ratsnest-control-plane-*.jar app.jar
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
CMD []
