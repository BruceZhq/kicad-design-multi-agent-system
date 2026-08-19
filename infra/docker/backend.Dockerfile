# RatsNest control plane — governance only.
# Build context: the directory containing RatsNest/ (same as runtime.Dockerfile):
#
#   docker build -f RatsNest/infra/docker/backend.Dockerfile -t ratsnest-backend .
FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /build
COPY RatsNest/backend/pom.xml .
RUN mvn -q -B dependency:go-offline
COPY RatsNest/backend/src ./src
RUN mvn -q -B package -DskipTests

FROM eclipse-temurin:17-jre
WORKDIR /app
COPY --from=build /build/target/ratsnest-control-plane-*.jar app.jar
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "app.jar"]
