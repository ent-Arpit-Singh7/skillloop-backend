-- SkillLoop MVP scope update (2026-11-13)
-- BACK UP THE DATABASE BEFORE RUNNING THIS MIGRATION.
-- This is additive/non-destructive: no User, Connection, Conversation, or Message rows are deleted.

ALTER TABLE "User" ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE "User" DROP CONSTRAINT IF EXISTS "User_account_status_check";
ALTER TABLE "User" DROP CONSTRAINT IF EXISTS user_account_status_check;
ALTER TABLE "User" ADD CONSTRAINT user_account_status_check
    CHECK (account_status IN ('Active', 'Suspended', 'Blocked', 'Deleted'));

-- Keep the learning connection workflow, but stop creating new chat conversations.
CREATE OR REPLACE FUNCTION fn_accept_connection_request(p_request_id INTEGER, p_receiver_id INTEGER)
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
    PERFORM fn_require_active_user(v_req.sender_id);

    INSERT INTO Connection (user_one_id, user_two_id)
    VALUES (LEAST(v_req.sender_id, v_req.receiver_id), GREATEST(v_req.sender_id, v_req.receiver_id))
    ON CONFLICT (user_one_id, user_two_id) DO NOTHING;

    SELECT connection_id INTO v_conn FROM Connection
    WHERE user_one_id = LEAST(v_req.sender_id, v_req.receiver_id)
      AND user_two_id = GREATEST(v_req.sender_id, v_req.receiver_id);

    UPDATE ConnectionRequest SET status = 'Accepted', connection_id = v_conn
    WHERE request_id = p_request_id;

    RETURN v_conn;
END $$;
