-- Independent mutual connection requests for SkillLoop.
-- Additive migration: preserve existing Connection, Request, Chat, Session,
-- Review, and Notification records.

CREATE TABLE IF NOT EXISTS networkconnectionrequest (
    request_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sender_id INTEGER NOT NULL REFERENCES "User"(user_id),
    receiver_id INTEGER NOT NULL REFERENCES "User"(user_id),
    optional_message TEXT,
    request_date TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status VARCHAR(20) NOT NULL DEFAULT 'Pending',
    connection_id INTEGER REFERENCES connection(connection_id),
    CONSTRAINT ck_network_connection_request_status
        CHECK (status IN ('Pending', 'Accepted', 'Rejected', 'Cancelled')),
    CONSTRAINT ck_network_connection_request_not_self
        CHECK (sender_id <> receiver_id),
    CONSTRAINT ck_network_connection_request_connection
        CHECK (
            (status = 'Accepted' AND connection_id IS NOT NULL)
            OR (status <> 'Accepted' AND connection_id IS NULL)
        )
);

-- Prevent two pending invitations in either direction for the same pair.
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_network_connection_request
    ON networkconnectionrequest (
        LEAST(sender_id, receiver_id),
        GREATEST(sender_id, receiver_id)
    )
    WHERE status = 'Pending';

CREATE INDEX IF NOT EXISTS ix_networkconnectionrequest_sender
    ON networkconnectionrequest(sender_id, status, request_id DESC);
CREATE INDEX IF NOT EXISTS ix_networkconnectionrequest_receiver
    ON networkconnectionrequest(receiver_id, status, request_id DESC);

ALTER TABLE connection
    ADD COLUMN IF NOT EXISTS status VARCHAR(20);
ALTER TABLE connection
    ADD COLUMN IF NOT EXISTS removed_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE connection
    ADD COLUMN IF NOT EXISTS removed_by_id INTEGER;

UPDATE connection
SET status = 'Active'
WHERE status IS NULL;

ALTER TABLE connection
    ALTER COLUMN status SET NOT NULL;
ALTER TABLE connection
    ALTER COLUMN status SET DEFAULT 'Active';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_connection_status'
          AND conrelid = 'connection'::regclass
    ) THEN
        ALTER TABLE connection
            ADD CONSTRAINT ck_connection_status
            CHECK (status IN ('Active', 'Removed'));
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_connection_removed_by'
          AND conrelid = 'connection'::regclass
    ) THEN
        ALTER TABLE connection
            ADD CONSTRAINT fk_connection_removed_by
            FOREIGN KEY (removed_by_id) REFERENCES "User"(user_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_connection_active_pair
    ON connection(user_one_id, user_two_id)
    WHERE status = 'Active';

-- Keep existing notification types. Independent connection requests reuse
-- request_received/request_accepted/request_rejected and reference the new
-- NetworkConnectionRequest entity.
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'notification'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%related_entity_type%'
    LOOP
        EXECUTE format(
            'ALTER TABLE notification DROP CONSTRAINT IF EXISTS %I',
            r.conname
        );
    END LOOP;
END $$;
ALTER TABLE notification
    ADD CONSTRAINT notification_related_entity_type_check
    CHECK (related_entity_type IN (
        'ConnectionRequest',
        'NetworkConnectionRequest',
        'Session',
        'Message',
        'Review'
    ));

CREATE OR REPLACE FUNCTION fn_validate_notification_entity() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE v_ok BOOLEAN;
BEGIN
    IF NEW.related_entity_type IS NULL THEN
        RETURN NEW;
    END IF;
    v_ok := CASE NEW.related_entity_type
        WHEN 'ConnectionRequest' THEN EXISTS (
            SELECT 1 FROM ConnectionRequest WHERE request_id = NEW.related_entity_id
        )
        WHEN 'NetworkConnectionRequest' THEN EXISTS (
            SELECT 1 FROM networkconnectionrequest WHERE request_id = NEW.related_entity_id
        )
        WHEN 'Session' THEN EXISTS (
            SELECT 1 FROM Session WHERE session_id = NEW.related_entity_id
        )
        WHEN 'Message' THEN EXISTS (
            SELECT 1 FROM Message WHERE message_id = NEW.related_entity_id
        )
        WHEN 'Review' THEN EXISTS (
            SELECT 1 FROM Review WHERE review_id = NEW.related_entity_id
        )
    END;
    IF NOT v_ok THEN
        RAISE EXCEPTION 'The related % does not exist.', NEW.related_entity_type;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_validate_notification_entity ON notification;
CREATE TRIGGER trg_validate_notification_entity
BEFORE INSERT ON notification
FOR EACH ROW EXECUTE FUNCTION fn_validate_notification_entity();

-- Existing skill-based requests continue to create/reuse canonical connections
-- and must reactivate a connection that was previously removed.
CREATE OR REPLACE FUNCTION fn_accept_connection_request(p_request_id INTEGER, p_receiver_id INTEGER)
RETURNS INTEGER
LANGUAGE plpgsql AS $$
DECLARE
    v_req ConnectionRequest%ROWTYPE;
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
    PERFORM fn_require_active_user(v_req.sender_id);

    INSERT INTO Connection (user_one_id, user_two_id, status)
    VALUES (
        LEAST(v_req.sender_id, v_req.receiver_id),
        GREATEST(v_req.sender_id, v_req.receiver_id),
        'Active'
    )
    ON CONFLICT (user_one_id, user_two_id) DO UPDATE
    SET status = 'Active',
        removed_at = NULL,
        removed_by_id = NULL;

    SELECT connection_id INTO v_conn FROM Connection
    WHERE user_one_id = LEAST(v_req.sender_id, v_req.receiver_id)
      AND user_two_id = GREATEST(v_req.sender_id, v_req.receiver_id);

    UPDATE ConnectionRequest
    SET status = 'Accepted', connection_id = v_conn
    WHERE request_id = p_request_id;

    RETURN v_conn;
END $$;

-- Independent connections replace the obsolete accepted-learning-request
-- dependency for conversation and session creation. Keep historical records:
-- these triggers permit existing history to remain readable while refusing
-- new conversation, message, or session creation on Removed connections.
CREATE OR REPLACE FUNCTION validate_conversation_creation() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM Connection
        WHERE connection_id = NEW.connection_id
          AND status = 'Active'
    ) THEN
        RAISE EXCEPTION 'A conversation requires an active connection.';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION validate_message_sender() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE v_one INTEGER; v_two INTEGER; v_status VARCHAR(20);
BEGIN
    SELECT c.user_one_id, c.user_two_id, c.status
    INTO v_one, v_two, v_status
    FROM Conversation conv
    JOIN Connection c ON c.connection_id = conv.connection_id
    WHERE conv.conversation_id = NEW.conversation_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'The specified conversation does not exist.';
    END IF;
    IF v_status <> 'Active' THEN
        RAISE EXCEPTION 'Cannot send messages to a removed connection.';
    END IF;
    IF NEW.sender_id <> v_one AND NEW.sender_id <> v_two THEN
        RAISE EXCEPTION 'Only connection participants can send messages in this conversation.';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION validate_session_participants() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE v_one INTEGER; v_two INTEGER; v_status VARCHAR(20);
BEGIN
    SELECT user_one_id, user_two_id, status
    INTO v_one, v_two, v_status
    FROM Connection
    WHERE connection_id = NEW.connection_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'The specified connection does not exist.';
    END IF;
    IF v_status <> 'Active' THEN
        RAISE EXCEPTION 'A session requires an active connection.';
    END IF;
    IF LEAST(NEW.teacher_id, NEW.learner_id) <> v_one
       OR GREATEST(NEW.teacher_id, NEW.learner_id) <> v_two THEN
        RAISE EXCEPTION 'The teacher and learner must belong to the specified connection.';
    END IF;
    RETURN NEW;
END $$;
