package dev.zav.mothersledger;

import java.time.Instant;
import java.util.UUID;

public record QuestCompletion(
        UUID eventId,
        String sourceId,
        UUID teamId,
        String teamName,
        String questId,
        String questName,
        Instant completedAt
) {
}