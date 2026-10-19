-- Phase 1: Add injection cooldown column to memories table
-- Prevents hot memories from being re-injected within 2 hours
-- Applied: 2026-03-24

ALTER TABLE memories ADD COLUMN IF NOT EXISTS last_injected_at TIMESTAMPTZ;
