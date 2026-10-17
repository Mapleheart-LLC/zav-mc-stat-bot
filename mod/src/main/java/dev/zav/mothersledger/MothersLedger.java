package dev.zav.mothersledger;

import dev.architectury.event.EventResult;
import dev.ftb.mods.ftbquests.events.ObjectCompletedEvent;
import dev.ftb.mods.ftbquests.quest.Quest;
import dev.ftb.mods.ftbquests.quest.TeamData;

import java.time.Instant;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicBoolean;

public final class MothersLedger {
    public static final String MOD_ID = "mothersledger";

    private static final AtomicBoolean INITIALIZED = new AtomicBoolean();
    private static final System.Logger LOGGER = System.getLogger(MothersLedger.class.getName());

    private MothersLedger() {
    }

    public static void initialize() {
        if (!INITIALIZED.compareAndSet(false, true)) {
            return;
        }

        ObjectCompletedEvent.QUEST.register(event -> {
            TeamData team = event.getData();
            if (!team.getFile().isServerSide()) {
                return EventResult.pass();
            }

            Quest quest = event.getQuest();
            Instant completedAt = event.getTime() == null
                    ? Instant.now()
                    : event.getTime().toInstant();
            String teamName = team.getName().isBlank()
                    ? team.getTeamId().toString()
                    : team.getName();

            QuestCompletion completion = new QuestCompletion(
                    UUID.randomUUID(),
                    BridgeClient.sourceId(),
                    team.getTeamId(),
                    teamName,
                    Long.toUnsignedString(quest.getId()),
                    quest.getTitle().getString(),
                    completedAt
            );
            BridgeClient.publish(completion);
            LOGGER.log(System.Logger.Level.INFO,
                    "FTB quest completed: quest={0}, team={1}", completion.questId(), teamName);
            return EventResult.pass();
        });

        LOGGER.log(System.Logger.Level.INFO, "Loaded {0} FTB Quests integration", MOD_ID);
    }
}