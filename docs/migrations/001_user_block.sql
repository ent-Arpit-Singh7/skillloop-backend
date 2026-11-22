-- Canonical UserBlock migration for SkillLoop.
-- Apply once to an existing database; safe to re-run.
-- The older plural migration created a quoted public."UserBlock" table, while
-- the current ORM/schema use the unquoted PostgreSQL name public.userblock.
DO $$
BEGIN
    IF to_regclass('public.userblock') IS NULL
       AND to_regclass('public."UserBlock"') IS NOT NULL THEN
        ALTER TABLE public."UserBlock" RENAME TO userblock;
    ELSIF to_regclass('public.userblock') IS NOT NULL
       AND to_regclass('public."UserBlock"') IS NOT NULL THEN
        INSERT INTO public.userblock (blocker_id, blocked_id, created_at)
        SELECT blocker_id, blocked_id, created_at
        FROM public."UserBlock"
        ON CONFLICT (blocker_id, blocked_id) DO NOTHING;
        DROP TABLE public."UserBlock";
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS public.userblock (
    block_id    INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    blocker_id  INTEGER NOT NULL REFERENCES public."User"(user_id) ON DELETE CASCADE,
    blocked_id  INTEGER NOT NULL REFERENCES public."User"(user_id) ON DELETE CASCADE,
    created_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_userblock_not_self CHECK (blocker_id <> blocked_id),
    CONSTRAINT uq_userblock_pair UNIQUE (blocker_id, blocked_id)
);

CREATE INDEX IF NOT EXISTS ix_userblock_blocked_id ON public.userblock(blocked_id);
