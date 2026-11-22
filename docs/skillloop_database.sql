/* =====================================================================
   SKILLLOOP  -  PostgreSQL DATABASE  (MVP, build from zero)
   ---------------------------------------------------------------------
   HOW TO RUN
     1. Create an EMPTY database:      CREATE DATABASE skillloop;
     2. Run this file once:            psql -d skillloop -v ON_ERROR_STOP=1 -f skillloop_database.sql
     pgAdmin version: no BEGIN/COMMIT lines. If it fails halfway, delete the database,
     create it again empty, and re-run the whole file.

   WHAT IS INSIDE (in this order)
     PART 1  Extensions
     PART 2  The 16 tables
     PART 3  Indexes (uniqueness rules, search speed, foreign keys)
     PART 4  Business rules written as triggers, grouped by feature
     PART 5  View for teacher ratings
     PART 6  Seed data (language catalogue)
     PART 7  Helper function for accepting a request (optional, recommended)
     PART 8  Quick self-check

   NOTES FOR THE BACKEND (read once)
     * Emails are unique case-insensitively. Store and compare emails in lower case.
     * "Deleting" a teaching/learning skill = UPDATE ... SET is_active = FALSE.
       A real DELETE of a skill is always blocked by a trigger.
     * "Cancel request" = UPDATE ConnectionRequest SET status = 'Cancelled'.
     * Accept a request with:  SELECT fn_accept_connection_request(request_id, receiver_user_id);
       It creates or reuses the Connection and Conversation in the correct order.
     * Admin removing a reported Review (action "Content Removed"), inside ONE transaction:
           BEGIN;  SET LOCAL app.admin_removal = 'on';  DELETE FROM Review WHERE review_id = ?;  COMMIT;
     * Things the database CANNOT know and the backend must enforce:
           - who is logged in (only the receiver accepts/rejects, only the sender cancels,
             only the learner accepts/rejects a session, only the teacher cancels/completes it,
             only the message RECIPIENT marks a message as read)
           - Suspended/Blocked users must be refused at login
           - the meeting link is shown to the learner only after the session is Scheduled
           - creating Notification rows when an event happens
   ===================================================================== */



/* =====================================================================
   PART 1 - EXTENSIONS
   btree_gist : lets us forbid overlapping availability slots
   pg_trgm    : makes "name contains text" searches fast
   ===================================================================== */
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS pg_trgm;


/* =====================================================================
   PART 2 - TABLES  (created in dependency order)
   ===================================================================== */

-- ---------------------------------------------------------------------
-- 1. "User" : every registered person. One account can both teach and learn
--    (there is no separate Teacher/Learner type). Quoted because USER is a
--    reserved word in PostgreSQL.
-- ---------------------------------------------------------------------
CREATE TABLE "User" (
    user_id        INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    full_name      VARCHAR(100) NOT NULL CHECK (length(btrim(full_name)) > 0),
    email          VARCHAR(255) NOT NULL UNIQUE
                   CHECK (email LIKE '_%@_%._%'),            -- basic shape: a@b.c
    password_hash  VARCHAR(255) NOT NULL,                    -- hashed by the backend, never plain text
    profile_photo  TEXT,                                     -- URL / path, optional
    city           VARCHAR(100),
    country        VARCHAR(100),
    bio            TEXT,
    created_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    account_status VARCHAR(20) NOT NULL DEFAULT 'Active'
                   CHECK (account_status IN ('Active', 'Suspended', 'Blocked'))
);

-- ---------------------------------------------------------------------
-- 2. Admin : administrator accounts. Created internally (never through
--    normal registration), so they live in their own table.
-- ---------------------------------------------------------------------
CREATE TABLE Admin (
    admin_id      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    full_name     VARCHAR(100) NOT NULL CHECK (length(btrim(full_name)) > 0),
    email         VARCHAR(255) NOT NULL UNIQUE CHECK (email LIKE '_%@_%._%'),
    password_hash VARCHAR(255) NOT NULL,
    created_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------
-- 3. SkillCategory : categories managed only by Admin; users pick from them.
--    A category that still has skills cannot be deleted (default FK behaviour).
-- ---------------------------------------------------------------------
CREATE TABLE SkillCategory (
    category_id         INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category_name       VARCHAR(100) NOT NULL UNIQUE CHECK (length(btrim(category_name)) > 0),
    description         TEXT,
    created_by_admin_id INTEGER NOT NULL REFERENCES Admin(admin_id)
);

-- ---------------------------------------------------------------------
-- 4. TeachingSkill : skills a user can teach.
--    is_active = FALSE means "removed by the user" (history stays intact).
--    UNIQUE (teaching_skill_id, user_id) lets other tables prove that a
--    skill really belongs to a given user (composite foreign keys below).
-- ---------------------------------------------------------------------
CREATE TABLE TeachingSkill (
    teaching_skill_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id           INTEGER NOT NULL REFERENCES "User"(user_id),
    category_id       INTEGER NOT NULL REFERENCES SkillCategory(category_id),
    skill_name        VARCHAR(100) NOT NULL CHECK (length(btrim(skill_name)) > 0),
    skill_level       VARCHAR(20) NOT NULL
                      CHECK (skill_level IN ('Beginner', 'Intermediate', 'Advanced')),
    short_description TEXT,
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (teaching_skill_id, user_id)
);

-- ---------------------------------------------------------------------
-- 5. LearningSkill : skills a user wants to learn (same soft-delete idea).
-- ---------------------------------------------------------------------
CREATE TABLE LearningSkill (
    learning_skill_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id           INTEGER NOT NULL REFERENCES "User"(user_id),
    category_id       INTEGER NOT NULL REFERENCES SkillCategory(category_id),
    skill_name        VARCHAR(100) NOT NULL CHECK (length(btrim(skill_name)) > 0),
    current_level     VARCHAR(20) NOT NULL
                      CHECK (current_level IN ('Beginner', 'Intermediate', 'Advanced')),
    learning_goal     TEXT,
    is_active         BOOLEAN NOT NULL DEFAULT TRUE
);

-- ---------------------------------------------------------------------
-- 6. Language : fixed catalogue users choose from (filled in PART 6).
-- ---------------------------------------------------------------------
CREATE TABLE Language (
    language_id   INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    language_name VARCHAR(80) NOT NULL UNIQUE
);

-- ---------------------------------------------------------------------
-- 7. UserLanguage : which languages a user speaks (many-to-many).
--    The composite primary key stops the same language being added twice.
-- ---------------------------------------------------------------------
CREATE TABLE UserLanguage (
    user_id     INTEGER NOT NULL REFERENCES "User"(user_id),
    language_id INTEGER NOT NULL REFERENCES Language(language_id),
    PRIMARY KEY (user_id, language_id)
);

-- ---------------------------------------------------------------------
-- 8. Availability : weekly time slots. Many slots per day are allowed,
--    but two slots of the same user on the same day cannot overlap.
-- ---------------------------------------------------------------------
CREATE TABLE Availability (
    availability_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES "User"(user_id),
    day_of_week     VARCHAR(15) NOT NULL
                    CHECK (day_of_week IN ('Monday','Tuesday','Wednesday','Thursday',
                                           'Friday','Saturday','Sunday')),
    start_time      TIME NOT NULL,
    end_time        TIME NOT NULL,
    CHECK (end_time > start_time),
    -- no overlapping slots for the same user on the same day
    CONSTRAINT ex_availability_no_overlap
        EXCLUDE USING gist (
            user_id     WITH =,
            day_of_week WITH =,
            tsrange(DATE '2000-01-01' + start_time, DATE '2000-01-01' + end_time) WITH &&
        )
);

-- ---------------------------------------------------------------------
-- 9. Connection : created automatically when a request is accepted.
--    The two user ids are stored in ascending order, so the pair (A,B)
--    can never also exist as (B,A).
-- ---------------------------------------------------------------------
CREATE TABLE Connection (
    connection_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_one_id   INTEGER NOT NULL REFERENCES "User"(user_id),
    user_two_id   INTEGER NOT NULL REFERENCES "User"(user_id),
    created_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (user_one_id < user_two_id),          -- different users + fixed order
    UNIQUE (user_one_id, user_two_id)
);

-- ---------------------------------------------------------------------
-- 10. ConnectionRequest : "I want to learn <your skill>".
--     Statuses: Pending -> Accepted | Rejected | Cancelled (then final).
--     The composite foreign key guarantees the skill belongs to the receiver.
--     connection_id is filled only when the request is Accepted.
-- ---------------------------------------------------------------------
CREATE TABLE ConnectionRequest (
    request_id        INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sender_id         INTEGER NOT NULL REFERENCES "User"(user_id),
    receiver_id       INTEGER NOT NULL REFERENCES "User"(user_id),
    teaching_skill_id INTEGER NOT NULL,
    optional_message  TEXT,
    request_date      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status            VARCHAR(20) NOT NULL DEFAULT 'Pending'
                      CHECK (status IN ('Pending', 'Accepted', 'Rejected', 'Cancelled')),
    connection_id     INTEGER REFERENCES Connection(connection_id),

    FOREIGN KEY (teaching_skill_id, receiver_id)
        REFERENCES TeachingSkill(teaching_skill_id, user_id),

    CHECK (sender_id <> receiver_id),            -- cannot request yourself
    CHECK ((status = 'Accepted' AND connection_id IS NOT NULL)
        OR (status <> 'Accepted' AND connection_id IS NULL))
);

-- ---------------------------------------------------------------------
-- 11. Conversation : exactly one chat per connection.
-- ---------------------------------------------------------------------
CREATE TABLE Conversation (
    conversation_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    connection_id   INTEGER NOT NULL UNIQUE REFERENCES Connection(connection_id),
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------
-- 12. Message : text-only chat. No edit, no delete (enforced in PART 4).
--     The receiver is "the other person of the connection", so there is
--     no receiver column.
-- ---------------------------------------------------------------------
CREATE TABLE Message (
    message_id      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES Conversation(conversation_id),
    sender_id       INTEGER NOT NULL REFERENCES "User"(user_id),
    message_text    TEXT NOT NULL CHECK (length(btrim(message_text)) > 0),
    sent_at         TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_read         BOOLEAN NOT NULL DEFAULT FALSE
);

-- ---------------------------------------------------------------------
-- 13. Session : online learning session proposed by the teacher.
--     Pending -> Scheduled | Rejected ;  Scheduled -> Cancelled | Completed.
--     The composite foreign key guarantees the teacher owns the skill.
--     UNIQUE (session_id, learner_id, teacher_id) lets Review prove that
--     the reviewer really is the learner of that session.
-- ---------------------------------------------------------------------
CREATE TABLE Session (
    session_id        INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    connection_id     INTEGER NOT NULL REFERENCES Connection(connection_id),
    teaching_skill_id INTEGER NOT NULL,
    teacher_id        INTEGER NOT NULL REFERENCES "User"(user_id),
    learner_id        INTEGER NOT NULL REFERENCES "User"(user_id),
    topic             VARCHAR(200) NOT NULL CHECK (length(btrim(topic)) > 0),
    session_date      DATE NOT NULL,
    start_time        TIME NOT NULL,
    duration_minutes  INTEGER NOT NULL CHECK (duration_minutes > 0),
    meeting_link      TEXT NOT NULL CHECK (length(btrim(meeting_link)) > 0),
    optional_note     TEXT,
    status            VARCHAR(20) NOT NULL DEFAULT 'Pending'
                      CHECK (status IN ('Pending','Scheduled','Rejected','Cancelled','Completed')),
    created_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (teaching_skill_id, teacher_id)
        REFERENCES TeachingSkill(teaching_skill_id, user_id),

    CHECK (teacher_id <> learner_id),
    UNIQUE (session_id, learner_id, teacher_id)
);

-- ---------------------------------------------------------------------
-- 14. Review : learner rates the teacher after a Completed session.
--     One review per session (UNIQUE), rating 1-5, text optional.
-- ---------------------------------------------------------------------
CREATE TABLE Review (
    review_id      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id     INTEGER NOT NULL UNIQUE,
    learner_id     INTEGER NOT NULL REFERENCES "User"(user_id),
    teacher_id     INTEGER NOT NULL REFERENCES "User"(user_id),
    rating         INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    written_review TEXT,
    created_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (session_id, learner_id, teacher_id)
        REFERENCES Session(session_id, learner_id, teacher_id),

    CHECK (learner_id <> teacher_id)
);

-- ---------------------------------------------------------------------
-- 15. Notification : in-app notifications (exactly the 9 supported events).
--     related_entity_type / related_entity_id point to the record that
--     caused it; both are filled or both are empty (checked in PART 4).
--     read_at IS NULL = unread.
-- ---------------------------------------------------------------------
CREATE TABLE Notification (
    notification_id     INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recipient_id        INTEGER NOT NULL REFERENCES "User"(user_id),
    notification_type   VARCHAR(40) NOT NULL
                        CHECK (notification_type IN (
                            'request_received',    -- receiver: new learning/connection request
                            'request_accepted',    -- sender:   request accepted
                            'request_rejected',    -- sender:   request rejected
                            'session_requested',   -- learner:  teacher proposed a session
                            'session_accepted',    -- teacher:  learner accepted the session
                            'session_rejected',    -- teacher:  learner rejected the session
                            'session_cancelled',   -- learner:  teacher cancelled a scheduled session
                            'new_message',         -- recipient: new chat message
                            'new_review_received'  -- teacher:  learner submitted a review
                        )),
    title               VARCHAR(150) NOT NULL CHECK (length(btrim(title)) > 0),
    message             TEXT NOT NULL CHECK (length(btrim(message)) > 0),
    related_entity_type VARCHAR(30)
                        CHECK (related_entity_type IN ('ConnectionRequest','Session','Message','Review')),
    related_entity_id   INTEGER,
    created_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    read_at             TIMESTAMP WITH TIME ZONE,

    CHECK ((related_entity_type IS NULL) = (related_entity_id IS NULL)),
    CONSTRAINT chk_notification_read_at CHECK (read_at IS NULL OR read_at >= created_at)
);

-- ---------------------------------------------------------------------
-- 16. Report : a user reports either ANOTHER USER or ONE piece of content
--     (never both, never neither). Admin reviews it.
--     Status flow: Pending -> Under Review -> Resolved | Dismissed.
-- ---------------------------------------------------------------------
CREATE TABLE Report (
    report_id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    reporter_user_id     INTEGER NOT NULL REFERENCES "User"(user_id),
    reported_user_id     INTEGER REFERENCES "User"(user_id),
    content_type         VARCHAR(30)
                         CHECK (content_type IN ('TeachingSkill','LearningSkill','Message','Review')),
    content_id           INTEGER,
    reason               VARCHAR(150) NOT NULL CHECK (length(btrim(reason)) > 0),
    description          TEXT,
    status               VARCHAR(20) NOT NULL DEFAULT 'Pending'
                         CHECK (status IN ('Pending','Under Review','Resolved','Dismissed')),
    reviewed_by_admin_id INTEGER REFERENCES Admin(admin_id),
    admin_action         TEXT
                         CHECK (admin_action IS NULL OR admin_action IN (
                             'No Action Required','Warning Issued','Content Removed',
                             'User Suspended','User Blocked','Other Action')),
    created_at           TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at          TIMESTAMP WITH TIME ZONE,

    -- exactly one target: a user OR a content record
    CHECK ((reported_user_id IS NOT NULL AND content_type IS NULL AND content_id IS NULL)
        OR (reported_user_id IS NULL AND content_type IS NOT NULL AND content_id IS NOT NULL)),
    -- cannot report yourself
    CONSTRAINT chk_report_not_self CHECK (reported_user_id IS NULL OR reported_user_id <> reporter_user_id),
    -- a closed report must say who closed it, what was done, and when
    CONSTRAINT chk_report_closed_has_admin CHECK (
        status NOT IN ('Resolved','Dismissed')
        OR (reviewed_by_admin_id IS NOT NULL AND admin_action IS NOT NULL AND reviewed_at IS NOT NULL))
);


/* =====================================================================
   PART 3 - INDEXES
   ===================================================================== */

-- 3.1 Uniqueness rules --------------------------------------------------
-- Emails unique ignoring letter case (ROHAN@x.com = rohan@x.com)
CREATE UNIQUE INDEX uq_user_email_lower  ON "User"(lower(email));
CREATE UNIQUE INDEX uq_admin_email_lower ON Admin(lower(email));

-- A user cannot list the same ACTIVE skill twice (a removed skill can be re-added)
CREATE UNIQUE INDEX uq_teachingskill_user_name ON TeachingSkill(user_id, lower(skill_name)) WHERE is_active;
CREATE UNIQUE INDEX uq_learningskill_user_name ON LearningSkill(user_id, lower(skill_name)) WHERE is_active;

-- No duplicate PENDING request for the same sender + receiver + skill.
-- (A new request is allowed after Rejected / Cancelled.)
CREATE UNIQUE INDEX uq_pending_connection_request
    ON ConnectionRequest(sender_id, receiver_id, teaching_skill_id)
    WHERE status = 'Pending';

-- 3.2 Search & discovery (name, category, level, language, day/time) ----
CREATE INDEX ix_user_name_trgm          ON "User"        USING gin (lower(full_name) gin_trgm_ops);
CREATE INDEX ix_teachingskill_name_trgm ON TeachingSkill USING gin (lower(skill_name) gin_trgm_ops) WHERE is_active;
CREATE INDEX ix_teachingskill_filter    ON TeachingSkill(category_id, skill_level) WHERE is_active;
CREATE INDEX ix_availability_search     ON Availability(day_of_week, start_time, end_time, user_id);

-- 3.3 Foreign-key columns (PostgreSQL does not index these automatically) -
CREATE INDEX ix_skillcategory_admin  ON SkillCategory(created_by_admin_id);
CREATE INDEX ix_teachingskill_user   ON TeachingSkill(user_id);
CREATE INDEX ix_learningskill_user   ON LearningSkill(user_id) WHERE is_active;
CREATE INDEX ix_learningskill_cat    ON LearningSkill(category_id);
CREATE INDEX ix_userlanguage_lang    ON UserLanguage(language_id);
CREATE INDEX ix_availability_user    ON Availability(user_id);
CREATE INDEX ix_connection_user_two  ON Connection(user_two_id);
CREATE INDEX ix_request_sender       ON ConnectionRequest(sender_id, status);
CREATE INDEX ix_request_receiver     ON ConnectionRequest(receiver_id, status);
CREATE INDEX ix_request_connection   ON ConnectionRequest(connection_id);
CREATE INDEX ix_request_skill        ON ConnectionRequest(teaching_skill_id);
CREATE INDEX ix_message_conv_sent    ON Message(conversation_id, sent_at);
CREATE INDEX ix_message_sender       ON Message(sender_id);
CREATE INDEX ix_session_connection   ON Session(connection_id);
CREATE INDEX ix_session_learner      ON Session(learner_id, status);
CREATE INDEX ix_session_teacher      ON Session(teacher_id, status);
CREATE INDEX ix_session_skill        ON Session(teaching_skill_id);
CREATE INDEX ix_review_learner       ON Review(learner_id);
CREATE INDEX ix_review_teacher       ON Review(teacher_id);
CREATE INDEX ix_notification_user    ON Notification(recipient_id, read_at, created_at DESC);
CREATE INDEX ix_report_reported_user ON Report(reported_user_id);
CREATE INDEX ix_report_reporter      ON Report(reporter_user_id);
CREATE INDEX ix_report_admin         ON Report(reviewed_by_admin_id);
CREATE INDEX ix_report_status        ON Report(status);


/* =====================================================================
   PART 4 - BUSINESS RULES (TRIGGERS)
   Each block = one function + the trigger(s) that use it.
   ===================================================================== */

-- ---------------------------------------------------------------------
-- 4.0 Shared helper: raise an error unless the user's account is Active.
--     Used so Suspended / Blocked users cannot send requests, messages
--     or propose sessions.
-- ---------------------------------------------------------------------
CREATE FUNCTION fn_require_active_user(p_user_id INTEGER) RETURNS VOID
LANGUAGE plpgsql AS $$
DECLARE v_status TEXT;
BEGIN
    SELECT account_status INTO v_status FROM "User" WHERE user_id = p_user_id;
    IF v_status IS DISTINCT FROM 'Active' THEN
        RAISE EXCEPTION 'Account is %; this action is not allowed.', COALESCE(v_status, 'missing');
    END IF;
END $$;

CREATE FUNCTION fn_active_sender() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN PERFORM fn_require_active_user(NEW.sender_id); RETURN NEW; END $$;

CREATE FUNCTION fn_active_teacher() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN PERFORM fn_require_active_user(NEW.teacher_id); RETURN NEW; END $$;

-- ---------------------------------------------------------------------
-- 4.1 CONNECTION REQUESTS
-- ---------------------------------------------------------------------

-- (a) Sender must be Active. (The active-skill check is in (b) below.)
CREATE TRIGGER trg_request_active_sender
    BEFORE INSERT ON ConnectionRequest
    FOR EACH ROW EXECUTE FUNCTION fn_active_sender();

CREATE FUNCTION fn_require_active_skill() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE v_active BOOLEAN;
BEGIN
    SELECT is_active INTO v_active FROM TeachingSkill WHERE teaching_skill_id = NEW.teaching_skill_id;
    IF v_active IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'This teaching skill is no longer active.';
    END IF;
    RETURN NEW;
END $$;


-- (b) Active-skill check + accepted request must point to a connection of exactly its two users.
--     The skill must be active (and owned by the receiver) when a request is CREATED,
--     when it is ACCEPTED (the teacher may have removed the skill meanwhile), or when
--     the skill/receiver is changed.
CREATE FUNCTION validate_connection_request() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    v_one          INTEGER;
    v_two          INTEGER;
    v_skill_active BOOLEAN;
    v_check_skill  BOOLEAN := FALSE;
BEGIN
    IF TG_OP = 'INSERT' THEN
        v_check_skill := TRUE;
    ELSIF (OLD.status IS DISTINCT FROM NEW.status AND NEW.status = 'Accepted')
          OR OLD.teaching_skill_id IS DISTINCT FROM NEW.teaching_skill_id
          OR OLD.receiver_id       IS DISTINCT FROM NEW.receiver_id THEN
        v_check_skill := TRUE;
    END IF;

    IF v_check_skill THEN
        SELECT is_active INTO v_skill_active FROM TeachingSkill
        WHERE teaching_skill_id = NEW.teaching_skill_id AND user_id = NEW.receiver_id
        FOR UPDATE;
        IF NOT FOUND OR v_skill_active IS NOT TRUE THEN
            RAISE EXCEPTION 'Cannot create or accept a request for an inactive teaching skill.';
        END IF;
    END IF;

    IF NEW.status = 'Accepted' THEN
        SELECT user_one_id, user_two_id INTO v_one, v_two
        FROM Connection WHERE connection_id = NEW.connection_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'The specified connection does not exist.';
        END IF;
        IF v_one <> LEAST(NEW.sender_id, NEW.receiver_id)
           OR v_two <> GREATEST(NEW.sender_id, NEW.receiver_id) THEN
            RAISE EXCEPTION 'The connection participants do not match the request sender and receiver.';
        END IF;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_validate_connection_request
    BEFORE INSERT OR UPDATE OF status, connection_id, sender_id, receiver_id ON ConnectionRequest
    FOR EACH ROW EXECUTE FUNCTION validate_connection_request();

-- (c) Status flow: only Pending can change (to Accepted/Rejected/Cancelled);
--     sender, receiver, skill and date can never be edited.
CREATE FUNCTION fn_request_guard() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.sender_id <> OLD.sender_id OR NEW.receiver_id <> OLD.receiver_id
       OR NEW.teaching_skill_id <> OLD.teaching_skill_id OR NEW.request_date <> OLD.request_date THEN
        RAISE EXCEPTION 'Sender, receiver, skill and date of a request cannot be changed.';
    END IF;
    IF NEW.status <> OLD.status AND OLD.status <> 'Pending' THEN
        RAISE EXCEPTION 'A % request cannot change status.', OLD.status;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_request_guard
    BEFORE UPDATE ON ConnectionRequest
    FOR EACH ROW EXECUTE FUNCTION fn_request_guard();

-- (d) An accepted request cannot be deleted/changed once a chat or session exists.
CREATE FUNCTION protect_accepted_connection_request() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'Accepted' THEN
        IF EXISTS (SELECT 1 FROM Conversation WHERE connection_id = OLD.connection_id)
           OR EXISTS (SELECT 1 FROM Session WHERE connection_id = OLD.connection_id) THEN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'An accepted request cannot be deleted after a conversation or session has been created.';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status
               OR NEW.connection_id IS DISTINCT FROM OLD.connection_id THEN
                RAISE EXCEPTION 'An accepted request cannot be changed after a conversation or session has been created.';
            END IF;
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_protect_accepted_connection_request
    BEFORE UPDATE OR DELETE ON ConnectionRequest
    FOR EACH ROW EXECUTE FUNCTION protect_accepted_connection_request();

-- ---------------------------------------------------------------------
-- 4.1b SKILLS : never physically deleted. "Remove" = SET is_active = FALSE.
-- ---------------------------------------------------------------------
CREATE FUNCTION prevent_skill_physical_delete() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Physical deletion of skills is not allowed. Set is_active = FALSE instead.';
END $$;

CREATE TRIGGER trg_prevent_teachingskill_delete
    BEFORE DELETE ON TeachingSkill
    FOR EACH ROW EXECUTE FUNCTION prevent_skill_physical_delete();

CREATE TRIGGER trg_prevent_learningskill_delete
    BEFORE DELETE ON LearningSkill
    FOR EACH ROW EXECUTE FUNCTION prevent_skill_physical_delete();

-- ---------------------------------------------------------------------
-- 4.2 CONNECTION : participants cannot be changed once anything depends on it
-- ---------------------------------------------------------------------
CREATE FUNCTION protect_connection_participants() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.user_one_id IS DISTINCT FROM OLD.user_one_id
       OR NEW.user_two_id IS DISTINCT FROM OLD.user_two_id THEN
        IF EXISTS (SELECT 1 FROM ConnectionRequest WHERE connection_id = OLD.connection_id)
           OR EXISTS (SELECT 1 FROM Conversation   WHERE connection_id = OLD.connection_id)
           OR EXISTS (SELECT 1 FROM Session        WHERE connection_id = OLD.connection_id) THEN
            RAISE EXCEPTION 'Connection participants cannot be changed after related records have been created.';
        END IF;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_protect_connection_participants
    BEFORE UPDATE OF user_one_id, user_two_id ON Connection
    FOR EACH ROW EXECUTE FUNCTION protect_connection_participants();

-- ---------------------------------------------------------------------
-- 4.3 CONVERSATION : only for a connection that has an Accepted request
-- ---------------------------------------------------------------------
CREATE FUNCTION validate_conversation_creation() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM ConnectionRequest
                   WHERE connection_id = NEW.connection_id AND status = 'Accepted') THEN
        RAISE EXCEPTION 'Conversation cannot be created without an accepted connection request.';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_validate_conversation_creation
    BEFORE INSERT OR UPDATE OF connection_id ON Conversation
    FOR EACH ROW EXECUTE FUNCTION validate_conversation_creation();

-- ---------------------------------------------------------------------
-- 4.4 MESSAGES
-- ---------------------------------------------------------------------
-- (a) Sender must be one of the two connection participants and Active.
CREATE FUNCTION validate_message_sender() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE v_one INTEGER; v_two INTEGER;
BEGIN
    SELECT c.user_one_id, c.user_two_id INTO v_one, v_two
    FROM Conversation conv JOIN Connection c ON c.connection_id = conv.connection_id
    WHERE conv.conversation_id = NEW.conversation_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'The specified conversation does not exist.';
    END IF;
    IF NEW.sender_id <> v_one AND NEW.sender_id <> v_two THEN
        RAISE EXCEPTION 'Only connection participants can send messages in this conversation.';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_validate_message_sender
    BEFORE INSERT OR UPDATE OF conversation_id, sender_id ON Message
    FOR EACH ROW EXECUTE FUNCTION validate_message_sender();

CREATE TRIGGER trg_message_active_sender
    BEFORE INSERT ON Message
    FOR EACH ROW EXECUTE FUNCTION fn_active_sender();

-- (b) No edit, no delete. The ONLY allowed change is is_read: FALSE -> TRUE.
CREATE FUNCTION fn_message_immutable() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Messages cannot be deleted.';
    END IF;
    IF NEW.message_text <> OLD.message_text OR NEW.sender_id <> OLD.sender_id
       OR NEW.conversation_id <> OLD.conversation_id OR NEW.sent_at <> OLD.sent_at THEN
        RAISE EXCEPTION 'Messages cannot be edited.';
    END IF;
    IF OLD.is_read AND NOT NEW.is_read THEN
        RAISE EXCEPTION 'A read message cannot be marked unread.';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_message_immutable
    BEFORE UPDATE OR DELETE ON Message
    FOR EACH ROW EXECUTE FUNCTION fn_message_immutable();

-- ---------------------------------------------------------------------
-- 4.5 SESSIONS
-- ---------------------------------------------------------------------
-- (a) Teacher + learner must be exactly the two users of the connection,
--     and the connection must have an Accepted request.
CREATE FUNCTION validate_session_participants() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE v_one INTEGER; v_two INTEGER;
BEGIN
    SELECT user_one_id, user_two_id INTO v_one, v_two
    FROM Connection WHERE connection_id = NEW.connection_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'The specified connection does not exist.';
    END IF;
    IF LEAST(NEW.teacher_id, NEW.learner_id) <> v_one
       OR GREATEST(NEW.teacher_id, NEW.learner_id) <> v_two THEN
        RAISE EXCEPTION 'The teacher and learner must belong to the specified connection.';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM ConnectionRequest
                   WHERE connection_id = NEW.connection_id AND status = 'Accepted') THEN
        RAISE EXCEPTION 'A session requires an accepted connection request.';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_validate_session_participants
    BEFORE INSERT OR UPDATE OF connection_id, teacher_id, learner_id ON Session
    FOR EACH ROW EXECUTE FUNCTION validate_session_participants();

-- (b) Proposing: teacher must be Active, skill must be active, and a new
--     session always starts as Pending.
CREATE TRIGGER trg_session_active_teacher
    BEFORE INSERT ON Session
    FOR EACH ROW EXECUTE FUNCTION fn_active_teacher();

CREATE TRIGGER trg_session_active_skill
    BEFORE INSERT ON Session
    FOR EACH ROW EXECUTE FUNCTION fn_require_active_skill();

CREATE FUNCTION fn_session_insert() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status <> 'Pending' THEN
        RAISE EXCEPTION 'A new session must start as Pending.';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_session_insert
    BEFORE INSERT ON Session
    FOR EACH ROW EXECUTE FUNCTION fn_session_insert();

-- (c) Status flow:  Pending   -> Scheduled | Rejected
--                   Scheduled -> Cancelled | Completed   (Completed only on/after the session date)
--                   Rejected / Cancelled / Completed are final.
CREATE FUNCTION validate_session_status_transition() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;

    IF OLD.status = 'Pending' THEN
        IF NEW.status NOT IN ('Scheduled', 'Rejected') THEN
            RAISE EXCEPTION 'Invalid session status transition from Pending to %.', NEW.status;
        END IF;
    ELSIF OLD.status = 'Scheduled' THEN
        IF NEW.status NOT IN ('Cancelled', 'Completed') THEN
            RAISE EXCEPTION 'Invalid session status transition from Scheduled to %.', NEW.status;
        END IF;
        IF NEW.status = 'Completed' AND NEW.session_date > CURRENT_DATE THEN
            RAISE EXCEPTION 'A session cannot be completed before its date.';
        END IF;
    ELSE
        RAISE EXCEPTION 'Session status cannot be changed after it becomes %.', OLD.status;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_validate_session_status_transition
    BEFORE UPDATE OF status ON Session
    FOR EACH ROW EXECUTE FUNCTION validate_session_status_transition();

-- (d) No rescheduling: who / when / which skill can never change after proposing.
CREATE FUNCTION fn_session_no_reschedule() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.session_date <> OLD.session_date OR NEW.start_time <> OLD.start_time
       OR NEW.duration_minutes <> OLD.duration_minutes OR NEW.teacher_id <> OLD.teacher_id
       OR NEW.learner_id <> OLD.learner_id OR NEW.connection_id <> OLD.connection_id
       OR NEW.teaching_skill_id <> OLD.teaching_skill_id THEN
        RAISE EXCEPTION 'A session cannot be rescheduled or reassigned.';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_session_no_reschedule
    BEFORE UPDATE ON Session
    FOR EACH ROW EXECUTE FUNCTION fn_session_no_reschedule();

-- ---------------------------------------------------------------------
-- 4.6 REVIEWS
-- ---------------------------------------------------------------------
-- (a) Only for a Completed session (learner/teacher match is enforced by the
--     composite foreign key; one-per-session by UNIQUE(session_id)).
CREATE FUNCTION validate_review() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM Session WHERE session_id = NEW.session_id AND status = 'Completed') THEN
        RAISE EXCEPTION 'A review can only be submitted for a completed session.';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_validate_review
    BEFORE INSERT OR UPDATE OF session_id ON Review
    FOR EACH ROW EXECUTE FUNCTION validate_review();

-- (b) Reviews are never edited. They can be deleted ONLY by the admin
--     "Content Removed" flow (backend sets app.admin_removal = 'on' for that transaction).
CREATE FUNCTION prevent_review_modification() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_setting('app.admin_removal', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'Reviews cannot be updated or deleted once submitted.';
END $$;

CREATE TRIGGER trg_prevent_review_modification
    BEFORE UPDATE OR DELETE ON Review
    FOR EACH ROW EXECUTE FUNCTION prevent_review_modification();

-- ---------------------------------------------------------------------
-- 4.7 NOTIFICATIONS
-- ---------------------------------------------------------------------
-- (a) The record the notification points to must exist.
CREATE FUNCTION fn_validate_notification_entity() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE v_ok BOOLEAN;
BEGIN
    IF NEW.related_entity_type IS NULL THEN
        RETURN NEW;
    END IF;
    v_ok := CASE NEW.related_entity_type
        WHEN 'ConnectionRequest' THEN EXISTS (SELECT 1 FROM ConnectionRequest WHERE request_id = NEW.related_entity_id)
        WHEN 'Session'           THEN EXISTS (SELECT 1 FROM Session           WHERE session_id = NEW.related_entity_id)
        WHEN 'Message'           THEN EXISTS (SELECT 1 FROM Message           WHERE message_id = NEW.related_entity_id)
        WHEN 'Review'            THEN EXISTS (SELECT 1 FROM Review            WHERE review_id  = NEW.related_entity_id)
    END;
    IF NOT v_ok THEN
        RAISE EXCEPTION 'The related % does not exist.', NEW.related_entity_type;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_validate_notification_entity
    BEFORE INSERT ON Notification
    FOR EACH ROW EXECUTE FUNCTION fn_validate_notification_entity();

-- (b) The only change allowed later is read_at (NULL -> timestamp, once).
CREATE FUNCTION fn_notification_guard() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.recipient_id <> OLD.recipient_id OR NEW.notification_type <> OLD.notification_type
       OR NEW.title <> OLD.title OR NEW.message <> OLD.message
       OR NEW.related_entity_type IS DISTINCT FROM OLD.related_entity_type
       OR NEW.related_entity_id   IS DISTINCT FROM OLD.related_entity_id
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'Only the read status of a notification can be changed.';
    END IF;
    IF OLD.read_at IS NOT NULL AND NEW.read_at IS DISTINCT FROM OLD.read_at THEN
        RAISE EXCEPTION 'A read notification cannot be changed back.';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_notification_guard
    BEFORE UPDATE ON Notification
    FOR EACH ROW EXECUTE FUNCTION fn_notification_guard();

-- ---------------------------------------------------------------------
-- 4.8 REPORTS : a reported piece of content must exist in its own table
-- ---------------------------------------------------------------------
CREATE FUNCTION validate_report_content() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE v_ok BOOLEAN;
BEGIN
    IF NEW.content_type IS NULL THEN
        RETURN NEW;                              -- report about a user: nothing to look up
    END IF;
    v_ok := CASE NEW.content_type
        WHEN 'TeachingSkill' THEN EXISTS (SELECT 1 FROM TeachingSkill WHERE teaching_skill_id = NEW.content_id)
        WHEN 'LearningSkill' THEN EXISTS (SELECT 1 FROM LearningSkill WHERE learning_skill_id = NEW.content_id)
        WHEN 'Message'       THEN EXISTS (SELECT 1 FROM Message       WHERE message_id        = NEW.content_id)
        WHEN 'Review'        THEN EXISTS (SELECT 1 FROM Review        WHERE review_id         = NEW.content_id)
    END;
    IF NOT v_ok THEN
        RAISE EXCEPTION 'The reported % does not exist.', NEW.content_type;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_validate_report_content
    BEFORE INSERT OR UPDATE OF content_type, content_id ON Report
    FOR EACH ROW EXECUTE FUNCTION validate_report_content();


/* =====================================================================
   PART 5 - VIEW: teacher rating summary
   One row per user: average stars + number of reviews received.
   Users with no reviews show NULL average and 0 count (as the API expects).
   ===================================================================== */
CREATE VIEW v_teacher_rating_summary AS
SELECT u.user_id AS teacher_id,
       ROUND(AVG(r.rating)::numeric, 2) AS average_rating,
       COUNT(r.review_id)               AS review_count
FROM "User" u
LEFT JOIN Review r ON r.teacher_id = u.user_id
GROUP BY u.user_id;


/* =====================================================================
   PART 6 - SEED DATA
   The Language table is a predefined catalogue, so it is filled here.
   Add or remove languages as you like (safe to re-run).
   ===================================================================== */
INSERT INTO Language (language_name) VALUES
 ('English'),('Hindi'),('Bengali'),('Telugu'),('Marathi'),('Tamil'),('Urdu'),('Gujarati'),
 ('Kannada'),('Malayalam'),('Punjabi'),('Odia'),('Assamese'),('Spanish'),('French'),
 ('German'),('Portuguese'),('Italian'),('Russian'),('Arabic'),('Mandarin Chinese'),
 ('Japanese'),('Korean'),('Turkish'),('Indonesian'),('Dutch'),('Swedish'),('Nepali')
ON CONFLICT (language_name) DO NOTHING;

/* ADMIN ACCOUNT (create yours once, with a password hash made by your backend):
   INSERT INTO Admin (full_name, email, password_hash)
   VALUES ('Your Name', 'admin@skillloop.com', '<bcrypt-hash-here>');
   Then create categories, e.g.:
   INSERT INTO SkillCategory (category_name, description, created_by_admin_id)
   VALUES ('Technology', 'Programming, data, design tools', 1);                                  */


/* =====================================================================
   PART 7 - HELPER: accept a learning request (optional but recommended)
   Does everything in the correct order, atomically:
     1. checks the caller is the receiver, Active, and the request is Pending
     2. creates the Connection, or REUSES it if the two users are already connected
     3. marks the request Accepted and links it to the connection
     4. creates the Conversation, or reuses the existing one
   Returns the connection_id.   Usage:  SELECT fn_accept_connection_request(12, 5);
   Rejecting / cancelling is a plain UPDATE of ConnectionRequest.status.
   ===================================================================== */
CREATE FUNCTION fn_accept_connection_request(p_request_id INTEGER, p_receiver_id INTEGER)
RETURNS INTEGER
LANGUAGE plpgsql AS $$
DECLARE
    v_req  ConnectionRequest%ROWTYPE;
    v_conn INTEGER;
BEGIN
    SELECT * INTO v_req FROM ConnectionRequest WHERE request_id = p_request_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Request % does not exist.', p_request_id;
    END IF;
    IF v_req.receiver_id <> p_receiver_id THEN
        RAISE EXCEPTION 'Only the receiver can accept this request.';
    END IF;
    IF v_req.status <> 'Pending' THEN
        RAISE EXCEPTION 'Only a Pending request can be accepted (current status: %).', v_req.status;
    END IF;
    PERFORM fn_require_active_user(p_receiver_id);

    INSERT INTO Connection (user_one_id, user_two_id)
    VALUES (LEAST(v_req.sender_id, v_req.receiver_id), GREATEST(v_req.sender_id, v_req.receiver_id))
    ON CONFLICT (user_one_id, user_two_id) DO NOTHING;

    SELECT connection_id INTO v_conn FROM Connection
    WHERE user_one_id = LEAST(v_req.sender_id, v_req.receiver_id)
      AND user_two_id = GREATEST(v_req.sender_id, v_req.receiver_id);

    UPDATE ConnectionRequest SET status = 'Accepted', connection_id = v_conn
    WHERE request_id = p_request_id;

    INSERT INTO Conversation (connection_id) VALUES (v_conn)
    ON CONFLICT (connection_id) DO NOTHING;

    RETURN v_conn;
END $$;


/* =====================================================================
   PART 8 - QUICK SELF-CHECK (shown after the script runs)
   Expected: 16 tables.
   ===================================================================== */
SELECT count(*) AS tables_created
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE';
