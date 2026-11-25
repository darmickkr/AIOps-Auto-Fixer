FROM eclipse-temurin:21-jre
WORKDIR /app
COPY target/gamezone-0.0.1-SNAPSHOT.jar app.jar
EXPOSE invalid-port
ENTRYPOINT ["java", "-jar", "app.jar"]