FROM eclipse-temurin:21-jdk-jammy
WORKDIR /app
COPY Main.java ./
RUN javac -d /app/out Main.java
ENV PORT=8080
EXPOSE 8080
ENTRYPOINT []
CMD ["java", "-cp", "/app/out", "Main"]
