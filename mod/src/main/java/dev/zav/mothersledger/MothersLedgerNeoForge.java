package dev.zav.mothersledger;

import net.neoforged.fml.common.Mod;

@Mod(MothersLedger.MOD_ID)
public final class MothersLedgerNeoForge {
    public MothersLedgerNeoForge() {
        MothersLedger.initialize();
    }
}