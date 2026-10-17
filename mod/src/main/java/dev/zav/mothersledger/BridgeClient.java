package dev.zav.mothersledger;

import com.google.gson.Gson;
import com.google.gson.JsonObject;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.time.Duration;
import java.time.Instant;
import java.util.HexFormat;
import java.util.UUID;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

final class BridgeClient {
    private static final String EVENT_PATH = "/api/v1/events";
    private static final Gson GSON = new Gson();
    private static final System.Logger LOGGER = System.getLogger(BridgeClient.class.getName());
    private static final HttpClient HTTP = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(5))
            .build();
    private static final ScheduledExecutorService WORKER = Executors.newSingleThreadScheduledExecutor(task -> {
        Thread thread = new Thread(task, "mothersledger-http");
        thread.setDaemon(true);
        return thread;
    });

    private static final String ENDPOINT = System.getenv("MOTHERS_LEDGER_ENDPOINT");
    private static final String SECRET = System.getenv("MOTHERS_LEDGER_SECRET");
    private static final String SOURCE_ID = System.getenv("MOTHERS_LEDGER_SOURCE_ID");
    private static volatile boolean missingConfigurationLogged;

    private BridgeClient() {
    }

    static String sourceId() {
        return SOURCE_ID == null || SOURCE_ID.isBlank() ? "unconfigured" : SOURCE_ID.trim();
    }

    static void publish(QuestCompletion completion) {
        if (!isConfigured()) {
            if (!missingConfigurationLogged) {
                missingConfigurationLogged = true;
                LOGGER.log(System.Logger.Level.WARNING,
                        "Quest webhook disabled; set MOTHERS_LEDGER_ENDPOINT, MOTHERS_LEDGER_SECRET, and MOTHERS_LEDGER_SOURCE_ID");
            }
            return;
        }
        WORKER.execute(() -> send(completion, 0));
    }

    private static boolean isConfigured() {
        if (ENDPOINT == null || ENDPOINT.isBlank() || SECRET == null || SECRET.isBlank()
                || SOURCE_ID == null || SOURCE_ID.isBlank()) {
            return false;
        }
        try {
            URI endpoint = URI.create(ENDPOINT);
            return "https".equalsIgnoreCase(endpoint.getScheme())
                    && EVENT_PATH.equals(endpoint.getRawPath())
                    && endpoint.getRawQuery() == null;
        } catch (IllegalArgumentException exception) {
            return false;
        }
    }

    private static void send(QuestCompletion completion, int attempt) {
        String body = GSON.toJson(toJson(completion));
        String timestamp = Long.toString(Instant.now().getEpochSecond());
        String nonce = UUID.randomUUID().toString();
        String signature;
        try {
            signature = sign("POST\n" + EVENT_PATH + "\n" + timestamp + "\n" + nonce + "\n" + body);
        } catch (GeneralSecurityException exception) {
            LOGGER.log(System.Logger.Level.ERROR, "Could not sign quest webhook", exception);
            return;
        }

        HttpRequest request = HttpRequest.newBuilder(URI.create(ENDPOINT))
                .timeout(Duration.ofSeconds(10))
                .header("Content-Type", "application/json")
                .header("X-Mothers-Ledger-Timestamp", timestamp)
                .header("X-Mothers-Ledger-Nonce", nonce)
                .header("X-Mothers-Ledger-Signature", signature)
                .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                .build();

        HTTP.sendAsync(request, HttpResponse.BodyHandlers.discarding())
                .whenComplete((response, error) -> {
                    if (error == null && response.statusCode() >= 200 && response.statusCode() < 300) {
                        LOGGER.log(System.Logger.Level.DEBUG, "Quest webhook accepted: {0}", completion.eventId());
                        return;
                    }

                    boolean retryable = error != null || response.statusCode() == 429 || response.statusCode() >= 500;
                    if (retryable && attempt < 5) {
                        long delaySeconds = Math.min(60, 1L << attempt);
                        WORKER.schedule(() -> send(completion, attempt + 1), delaySeconds, TimeUnit.SECONDS);
                        return;
                    }

                    if (error != null) {
                        LOGGER.log(System.Logger.Level.WARNING,
                                "Quest webhook failed after {0} attempts: {1}", attempt + 1, error.toString());
                    } else {
                        LOGGER.log(System.Logger.Level.WARNING,
                                "Quest webhook rejected: HTTP {0}", response.statusCode());
                    }
                });
    }

    private static JsonObject toJson(QuestCompletion completion) {
        JsonObject json = new JsonObject();
        json.addProperty("schema_version", 1);
        json.addProperty("event_id", completion.eventId().toString());
        json.addProperty("source_id", completion.sourceId());
        json.addProperty("event_type", "quest.completed");
        json.addProperty("team_id", completion.teamId().toString());
        json.addProperty("team_name", completion.teamName());
        json.addProperty("quest_id", completion.questId());
        json.addProperty("quest_name", completion.questName());
        json.addProperty("occurred_at", completion.completedAt().toString());
        return json;
    }

    private static String sign(String message) throws GeneralSecurityException {
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(SECRET.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
        return HexFormat.of().formatHex(mac.doFinal(message.getBytes(StandardCharsets.UTF_8)));
    }
}