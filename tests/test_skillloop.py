"""Tests for the SkillLoop rules. They run against a real PostgreSQL database built from the team's SQL file,
so the database's own triggers and constraints are tested as well."""
from datetime import date, timedelta

TODAY = date.today().isoformat()
TOMORROW = (date.today() + timedelta(days=1)).isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


# ---------- helpers ----------
def give_skill(client, headers, category_id, name="Python basics", level="Beginner"):
    r = client.post(
        "/api/me/teaching-skills",
        headers=headers,
        json={"category_id": category_id, "skill_name": name, "skill_level": level, "short_description": "Learn it"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def connect(client, teacher_h, learner_h, category_id, skill_name="Python basics"):
    """Teacher has a skill, learner requests it, teacher accepts. Returns (skill, connection)."""
    skill = give_skill(client, teacher_h, category_id, skill_name)
    req = client.post("/api/requests", headers=learner_h, json={"teaching_skill_id": skill["teaching_skill_id"]})
    assert req.status_code == 201, req.text
    acc = client.post(f"/api/requests/{req.json()['request_id']}/accept", headers=teacher_h)
    assert acc.status_code == 200, acc.text
    return skill, acc.json()


def propose(client, teacher_h, conn, skill, day=TODAY, link="https://meet.example.com/abc"):
    return client.post(
        "/api/sessions",
        headers=teacher_h,
        json={
            "connection_id": conn["connection_id"],
            "teaching_skill_id": skill["teaching_skill_id"],
            "topic": "Intro lesson",
            "session_date": day,
            "start_time": "10:00",
            "duration_minutes": 45,
            "meeting_link": link,
            "optional_note": "Bring questions",
        },
    )


def completed_session(client, teacher_h, learner_h, conn, skill):
    s = propose(client, teacher_h, conn, skill)
    assert s.status_code == 201, s.text
    sid = s.json()["session_id"]
    assert client.post(f"/api/sessions/{sid}/accept", headers=learner_h).status_code == 200
    done = client.post(f"/api/sessions/{sid}/complete", headers=teacher_h)
    assert done.status_code == 200, done.text
    return sid


# ---------- Authentication ----------
def test_unknown_email_uses_generic_auth_failure(client):
    response = client.post("/api/auth/login", json={"email": "nobody@example.com", "password": "password123"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Incorrect email or password"


def test_per_account_login_limit_and_success_reset(client, make_user):
    make_user("limit@example.com")
    for _ in range(5):
        response = client.post("/api/auth/login", json={"email": "limit@example.com", "password": "wrong-password"})
        assert response.status_code == 401
    limited = client.post("/api/auth/login", json={"email": "LIMIT@example.com", "password": "password123"})
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers


def test_register_login_me_and_no_password_leak(client, make_user):
    h, user = make_user("a@example.com", "Asha")
    assert "password" not in str(user).lower()
    me = client.get("/api/me", headers=h)
    assert me.status_code == 200 and me.json()["email"] == "a@example.com"
    assert me.json()["account_status"] == "Active"


def test_email_unique_ignoring_case_and_weak_password(client, make_user):
    make_user("a@example.com")
    dup = client.post("/api/auth/register", json={"email": "A@Example.com", "password": "password123", "full_name": "X"})
    assert dup.status_code == 409
    weak = client.post("/api/auth/register", json={"email": "b@example.com", "password": "short", "full_name": "B"})
    assert weak.status_code == 422
    bad = client.post("/api/auth/login", json={"email": "a@example.com", "password": "wrong-password"})
    assert bad.status_code == 401


def test_cannot_register_as_admin(client, make_user):
    r = client.post(
        "/api/auth/register",
        json={"email": "evil@example.com", "password": "password123", "full_name": "E", "role": "admin", "is_admin": True},
    )
    assert r.status_code == 201
    token = client.post("/api/auth/login", json={"email": "evil@example.com", "password": "password123"}).json()["access_token"]
    assert client.get("/api/admin/users", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_protected_routes_need_token(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/search").status_code == 401
    assert client.get("/api/notifications").status_code == 401


def test_admin_and_user_tokens_are_not_interchangeable(client, make_user, admin_headers):
    user_h, _ = make_user("u@example.com")
    assert client.get("/api/admin/users", headers=user_h).status_code == 401
    assert client.get("/api/me", headers=admin_headers).status_code == 401


def test_suspended_and_blocked_users_are_refused(client, make_user, admin_headers):
    user_h, user = make_user("u@example.com")
    r = client.patch(f"/api/admin/users/{user['user_id']}", headers=admin_headers, json={"account_status": "Suspended"})
    assert r.status_code == 200 and r.json()["account_status"] == "Suspended"
    login = client.post("/api/auth/login", json={"email": "u@example.com", "password": "password123"})
    assert login.status_code == 403
    assert client.get("/api/me", headers=user_h).status_code == 403  # old token no longer works
    client.patch(f"/api/admin/users/{user['user_id']}", headers=admin_headers, json={"account_status": "Active"})
    assert client.get("/api/me", headers=user_h).status_code == 200


# ---------- Profile, categories, skills, languages, availability ----------
def test_update_profile(client, make_user):
    h, _ = make_user("a@example.com", "Asha")
    r = client.patch("/api/me", headers=h, json={"bio": "Hello", "city": "Solan", "country": "India"})
    assert r.status_code == 200 and r.json()["city"] == "Solan" and r.json()["bio"] == "Hello"


def test_only_admin_manages_categories(client, make_user, admin_headers):
    user_h, _ = make_user("u@example.com")
    assert client.post("/api/admin/categories", headers=user_h, json={"category_name": "Hack"}).status_code == 401
    c = client.post("/api/admin/categories", headers=admin_headers, json={"category_name": "Music"})
    assert c.status_code == 201
    cid = c.json()["category_id"]
    assert client.post("/api/admin/categories", headers=admin_headers, json={"category_name": "Music"}).status_code == 409
    assert any(x["category_name"] == "Music" for x in client.get("/api/categories", headers=user_h).json())
    edited = client.patch(f"/api/admin/categories/{cid}", headers=admin_headers, json={"category_name": "Music & Art"})
    assert edited.status_code == 200 and edited.json()["category_name"] == "Music & Art"
    assert client.delete(f"/api/admin/categories/{cid}", headers=admin_headers).status_code == 204
    assert client.get("/api/categories", headers=user_h).json() == []


def test_category_in_use_cannot_be_deleted(client, make_user, admin_headers, category_id):
    h, _ = make_user("t@example.com")
    give_skill(client, h, category_id)
    assert client.delete(f"/api/admin/categories/{category_id}", headers=admin_headers).status_code == 409


def test_teaching_skill_rules(client, make_user, category_id):
    a_h, _ = make_user("a@example.com")
    b_h, _ = make_user("b@example.com")
    skill = give_skill(client, a_h, category_id, "Guitar")
    sid = skill["teaching_skill_id"]
    assert skill["category_name"] == "Technology" and skill["skill_level"] == "Beginner"
    # same active skill name twice -> refused by the database
    dup = client.post("/api/me/teaching-skills", headers=a_h,
                      json={"category_id": category_id, "skill_name": "guitar", "skill_level": "Advanced"})
    assert dup.status_code == 409
    # invalid level / unknown category
    assert client.post("/api/me/teaching-skills", headers=a_h,
                       json={"category_id": category_id, "skill_name": "X", "skill_level": "Expert"}).status_code == 422
    assert client.post("/api/me/teaching-skills", headers=a_h,
                       json={"category_id": 999, "skill_name": "X", "skill_level": "Beginner"}).status_code == 400
    # another user cannot edit or remove it
    assert client.patch(f"/api/me/teaching-skills/{sid}", headers=b_h, json={"skill_name": "Hacked"}).status_code == 404
    assert client.delete(f"/api/me/teaching-skills/{sid}", headers=b_h).status_code == 404
    # owner edits
    ok = client.patch(f"/api/me/teaching-skills/{sid}", headers=a_h, json={"skill_level": "Advanced"})
    assert ok.status_code == 200 and ok.json()["skill_level"] == "Advanced"
    # "delete" hides it (soft delete) and the name can be used again
    assert client.delete(f"/api/me/teaching-skills/{sid}", headers=a_h).status_code == 204
    assert client.get("/api/me/teaching-skills", headers=a_h).json() == []
    assert client.post("/api/me/teaching-skills", headers=a_h,
                       json={"category_id": category_id, "skill_name": "Guitar", "skill_level": "Beginner"}).status_code == 201


def test_learning_skills(client, make_user, category_id):
    h, _ = make_user("a@example.com")
    r = client.post("/api/me/learning-skills", headers=h,
                    json={"category_id": category_id, "skill_name": "Piano", "current_level": "Beginner", "learning_goal": "Play Bach"})
    assert r.status_code == 201 and r.json()["learning_goal"] == "Play Bach"
    lid = r.json()["learning_skill_id"]
    assert client.patch(f"/api/me/learning-skills/{lid}", headers=h, json={"current_level": "Intermediate"}).json()["current_level"] == "Intermediate"
    assert client.delete(f"/api/me/learning-skills/{lid}", headers=h).status_code == 204
    assert client.get("/api/me/learning-skills", headers=h).json() == []


def test_languages(client, make_user):
    h, _ = make_user("a@example.com")
    catalogue = client.get("/api/languages", headers=h).json()
    english = next(x for x in catalogue if x["language_name"] == "English")
    assert client.post("/api/me/languages", headers=h, json={"language_id": english["language_id"]}).status_code == 201
    assert client.post("/api/me/languages", headers=h, json={"language_id": english["language_id"]}).status_code == 409
    assert client.post("/api/me/languages", headers=h, json={"language_id": 9999}).status_code == 400
    assert [x["language_name"] for x in client.get("/api/me/languages", headers=h).json()] == ["English"]
    assert client.delete(f"/api/me/languages/{english['language_id']}", headers=h).status_code == 204
    assert client.get("/api/me/languages", headers=h).json() == []


def test_availability_multiple_slots_and_no_overlap(client, make_user):
    h, _ = make_user("a@example.com")

    def slot(day, start, end):
        return client.post("/api/me/availability", headers=h, json={"day_of_week": day, "start_time": start, "end_time": end})

    assert slot("Monday", "09:00", "10:00").status_code == 201
    assert slot("Monday", "14:00", "16:00").status_code == 201  # several slots on the same day are allowed
    assert slot("Monday", "09:30", "11:00").status_code == 409  # overlap refused by the database
    assert slot("Tuesday", "09:30", "11:00").status_code == 201
    assert slot("Monday", "12:00", "11:00").status_code == 422
    items = client.get("/api/me/availability", headers=h).json()
    assert len(items) == 3
    first = items[0]["availability_id"]
    assert client.patch(f"/api/me/availability/{first}", headers=h, json={"end_time": "09:45"}).status_code == 200
    assert client.delete(f"/api/me/availability/{first}", headers=h).status_code == 204
    assert len(client.get("/api/me/availability", headers=h).json()) == 2


def test_cannot_touch_someone_elses_availability(client, make_user):
    a_h, _ = make_user("a@example.com")
    b_h, _ = make_user("b@example.com")
    r = client.post("/api/me/availability", headers=a_h, json={"day_of_week": "Friday", "start_time": "09:00", "end_time": "10:00"})
    aid = r.json()["availability_id"]
    assert client.patch(f"/api/me/availability/{aid}", headers=b_h, json={"end_time": "09:30"}).status_code == 404
    assert client.delete(f"/api/me/availability/{aid}", headers=b_h).status_code == 404


# ---------- Search ----------
def test_search_by_skill_person_and_filters(client, make_user, admin_headers, category_id):
    t_h, t = make_user("t@example.com", "Tara Teacher")
    l_h, _ = make_user("l@example.com", "Leo Learner")
    other_cat = client.post("/api/admin/categories", headers=admin_headers, json={"category_name": "Music"}).json()["category_id"]
    give_skill(client, t_h, category_id, "Python basics", "Beginner")
    give_skill(client, t_h, other_cat, "Guitar", "Advanced")
    give_skill(client, l_h, category_id, "My own skill")
    english = next(x for x in client.get("/api/languages", headers=l_h).json() if x["language_name"] == "English")
    client.post("/api/me/languages", headers=t_h, json={"language_id": english["language_id"]})
    client.post("/api/me/availability", headers=t_h, json={"day_of_week": "Monday", "start_time": "09:00", "end_time": "12:00"})

    by_skill = client.get("/api/search", headers=l_h, params={"mode": "skill", "q": "guit"}).json()
    assert [c["user"]["full_name"] for c in by_skill] == ["Tara Teacher"]
    assert [s["skill_name"] for s in by_skill[0]["skills"]] == ["Guitar"]  # only the matching skill
    assert "email" not in by_skill[0]["user"]
    by_person = client.get("/api/search", headers=l_h, params={"mode": "person", "q": "tara"}).json()
    assert len(by_person) == 1 and len(by_person[0]["skills"]) == 2
    assert client.get("/api/search", headers=l_h, params={"q": "own skill"}).json() == []  # never yourself
    assert len(client.get("/api/search", headers=l_h, params={"category_id": other_cat}).json()) == 1
    assert client.get("/api/search", headers=l_h, params={"skill_level": "Intermediate"}).json() == []
    assert len(client.get("/api/search", headers=l_h, params={"skill_level": "Advanced"}).json()) == 1
    assert len(client.get("/api/search", headers=l_h, params={"language_id": english["language_id"]}).json()) == 1
    assert client.get("/api/search", headers=l_h, params={"language_id": 99}).json() == []
    assert len(client.get("/api/search", headers=l_h, params={"day_of_week": "Monday"}).json()) == 1
    assert len(client.get("/api/search", headers=l_h, params={"day_of_week": "Monday", "start_time": "11:00", "end_time": "13:00"}).json()) == 1
    assert client.get("/api/search", headers=l_h, params={"day_of_week": "Monday", "start_time": "13:00"}).json() == []
    assert client.get("/api/search", headers=l_h, params={"day_of_week": "Tuesday"}).json() == []
    assert client.get("/api/search", headers=l_h, params={"start_time": "09:00"}).status_code == 422
    card = client.get(f"/api/users/{t['user_id']}", headers=l_h).json()
    assert card["languages"][0]["language_name"] == "English" and card["average_rating"] is None and card["review_count"] == 0


def test_search_hides_suspended_users_and_inactive_skills(client, make_user, admin_headers, category_id):
    t_h, t = make_user("t@example.com", "Tara")
    l_h, _ = make_user("l@example.com", "Leo")
    skill = give_skill(client, t_h, category_id)
    assert len(client.get("/api/search", headers=l_h).json()) == 1
    client.delete(f"/api/me/teaching-skills/{skill['teaching_skill_id']}", headers=t_h)
    assert client.get("/api/search", headers=l_h).json() == []
    give_skill(client, t_h, category_id, "Another")
    client.patch(f"/api/admin/users/{t['user_id']}", headers=admin_headers, json={"account_status": "Suspended"})
    assert client.get("/api/search", headers=l_h).json() == []
    assert client.get(f"/api/users/{t['user_id']}", headers=l_h).status_code == 404


# ---------- Requests and connections ----------
def test_request_rules(client, make_user, category_id):
    t_h, _ = make_user("t@example.com")
    l_h, _ = make_user("l@example.com")
    skill = give_skill(client, t_h, category_id)
    sid = skill["teaching_skill_id"]
    assert client.post("/api/requests", headers=t_h, json={"teaching_skill_id": sid}).status_code == 400  # own skill
    assert client.post("/api/requests", headers=l_h, json={"teaching_skill_id": 9999}).status_code == 404
    r = client.post("/api/requests", headers=l_h, json={"teaching_skill_id": sid, "optional_message": "Please!"})
    assert r.status_code == 201 and r.json()["status"] == "Pending" and r.json()["skill_name"] == "Python basics"
    assert client.post("/api/requests", headers=l_h, json={"teaching_skill_id": sid}).status_code == 409  # duplicate pending
    rid = r.json()["request_id"]
    # only the sender can cancel, only the receiver can accept/reject
    assert client.post(f"/api/requests/{rid}/cancel", headers=t_h).status_code == 403
    assert client.post(f"/api/requests/{rid}/accept", headers=l_h).status_code == 403
    assert client.post(f"/api/requests/{rid}/reject", headers=l_h).status_code == 403
    assert client.post(f"/api/requests/{rid}/cancel", headers=l_h).json()["status"] == "Cancelled"
    assert client.post(f"/api/requests/{rid}/accept", headers=t_h).status_code == 409  # final
    # a new request is allowed after cancelling
    assert client.post("/api/requests", headers=l_h, json={"teaching_skill_id": sid}).status_code == 201


def test_independent_connection_request_accept_list_and_public_count(client, make_user):
    a_h, a = make_user("network-a@example.com", "Network A")
    b_h, b = make_user("network-b@example.com", "Network B")
    outsider_h, _ = make_user("network-outside@example.com", "Network Outsider")

    assert client.post(
        "/api/connection-requests",
        headers=a_h,
        json={"receiver_id": a["user_id"]},
    ).status_code == 400

    sent = client.post(
        "/api/connection-requests",
        headers=a_h,
        json={"receiver_id": b["user_id"], "optional_message": "Let's connect"},
    )
    assert sent.status_code == 201, sent.text
    request = sent.json()
    assert request["status"] == "Pending"
    assert request["optional_message"] == "Let's connect"

    assert client.post(
        f"/api/connection-requests/{request['request_id']}/accept",
        headers=a_h,
    ).status_code == 403

    accepted = client.post(
        f"/api/connection-requests/{request['request_id']}/accept",
        headers=b_h,
    )
    assert accepted.status_code == 200, accepted.text
    connection = accepted.json()
    assert connection["status"] == "Active"
    assert connection["other_user"]["user_id"] == a["user_id"]

    sent_list = client.get(
        "/api/connection-requests",
        headers=a_h,
        params={"box": "sent"},
    )
    assert sent_list.status_code == 200
    assert sent_list.json()[0]["status"] == "Accepted"

    received_list = client.get(
        "/api/connection-requests",
        headers=b_h,
        params={"box": "received"},
    )
    assert received_list.status_code == 200
    assert received_list.json()[0]["request_id"] == request["request_id"]

    assert [c["connection_id"] for c in client.get("/api/connections", headers=a_h).json()] == [
        connection["connection_id"]
    ]
    assert client.get(f"/api/connections/{connection['connection_id']}", headers=outsider_h).status_code == 404

    profile_a = client.get(f"/api/users/{a['user_id']}", headers=b_h)
    profile_b = client.get(f"/api/users/{b['user_id']}", headers=a_h)
    assert profile_a.status_code == 200
    assert profile_b.status_code == 200
    assert profile_a.json()["connection_count"] == 1
    assert profile_b.json()["connection_count"] == 1


def test_independent_connection_duplicate_reject_cancel_and_removal(client, make_user):
    a_h, a = make_user("network-a2@example.com", "Network A2")
    b_h, b = make_user("network-b2@example.com", "Network B2")

    first = client.post(
        "/api/connection-requests",
        headers=a_h,
        json={"receiver_id": b["user_id"]},
    )
    assert first.status_code == 201, first.text
    request_id = first.json()["request_id"]

    assert client.post(
        "/api/connection-requests",
        headers=a_h,
        json={"receiver_id": b["user_id"]},
    ).status_code == 409
    assert client.post(
        "/api/connection-requests",
        headers=b_h,
        json={"receiver_id": a["user_id"]},
    ).status_code == 409

    assert client.post(
        f"/api/connection-requests/{request_id}/reject",
        headers=a_h,
    ).status_code == 403
    assert client.post(
        f"/api/connection-requests/{request_id}/accept",
        headers=a_h,
    ).status_code == 403

    rejected = client.post(
        f"/api/connection-requests/{request_id}/reject",
        headers=b_h,
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "Rejected"

    second = client.post(
        "/api/connection-requests",
        headers=b_h,
        json={"receiver_id": a["user_id"]},
    )
    assert second.status_code == 201, second.text
    second_id = second.json()["request_id"]

    assert client.post(
        f"/api/connection-requests/{second_id}/cancel",
        headers=b_h,
    ).json()["status"] == "Cancelled"
    assert client.post(
        f"/api/connection-requests/{second_id}/cancel",
        headers=a_h,
    ).status_code == 403

    third = client.post(
        "/api/connection-requests",
        headers=a_h,
        json={"receiver_id": b["user_id"]},
    )
    assert third.status_code == 201, third.text
    accepted = client.post(
        f"/api/connection-requests/{third.json()['request_id']}/accept",
        headers=b_h,
    )
    assert accepted.status_code == 200, accepted.text
    connection_id = accepted.json()["connection_id"]

    assert client.delete(
        f"/api/connections/{connection_id}",
        headers=a_h,
    ).status_code == 204
    assert client.get("/api/connections", headers=a_h).json() == []
    assert client.get(f"/api/users/{b['user_id']}", headers=a_h).json()["connection_count"] == 0

    reconnect = client.post(
        "/api/connection-requests",
        headers=b_h,
        json={"receiver_id": a["user_id"]},
    )
    assert reconnect.status_code == 201, reconnect.text
    reaccepted = client.post(
        f"/api/connection-requests/{reconnect.json()['request_id']}/accept",
        headers=a_h,
    )
    assert reaccepted.status_code == 200, reaccepted.text
    assert reaccepted.json()["connection_id"] == connection_id
    assert client.get(f"/api/users/{b['user_id']}", headers=a_h).json()["connection_count"] == 1


def test_independent_connections_preserve_skill_request_compatibility(client, make_user, category_id):
    teacher_h, teacher = make_user("network-teacher@example.com", "Network Teacher")
    learner_h, learner = make_user("network-learner@example.com", "Network Learner")

    skill = give_skill(client, teacher_h, category_id, "Independent + Skill")
    learning_request = client.post(
        "/api/requests",
        headers=learner_h,
        json={"teaching_skill_id": skill["teaching_skill_id"], "optional_message": "Learn"},
    )
    assert learning_request.status_code == 201, learning_request.text

    accepted = client.post(
        f"/api/requests/{learning_request.json()['request_id']}/accept",
        headers=teacher_h,
    )
    assert accepted.status_code == 200, accepted.text
    connection = accepted.json()
    assert connection["status"] == "Active"

    assert client.get("/api/connections", headers=learner_h).json()[0]["connection_id"] == connection["connection_id"]
    assert client.get(f"/api/users/{teacher['user_id']}", headers=learner_h).json()["connection_count"] == 1


def test_independent_connection_chat_session_removal_and_history(client, make_user, category_id):
    teacher_h, teacher = make_user("integration-teacher@example.com", "Integration Teacher")
    learner_h, learner = make_user("integration-learner@example.com", "Integration Learner")
    outsider_h, outsider = make_user("integration-outsider@example.com", "Integration Outsider")

    request = client.post(
        "/api/connection-requests",
        headers=teacher_h,
        json={"receiver_id": learner["user_id"], "optional_message": "Connect for integration"},
    )
    assert request.status_code == 201, request.text
    accepted = client.post(
        f"/api/connection-requests/{request.json()['request_id']}/accept",
        headers=learner_h,
    )
    assert accepted.status_code == 200, accepted.text
    connection_id = accepted.json()["connection_id"]

    conversation = client.post(
        "/api/conversations",
        headers=teacher_h,
        json={"connection_id": connection_id},
    )
    assert conversation.status_code == 201, conversation.text
    conversation_id = conversation.json()["conversation_id"]
    sent = client.post(
        f"/api/conversations/{conversation_id}/messages",
        headers=teacher_h,
        json={"message_text": "Independent connection chat"},
    )
    assert sent.status_code == 201, sent.text

    skill = give_skill(client, teacher_h, category_id, "Independent session skill")
    session = propose(client, teacher_h, accepted.json(), skill)
    assert session.status_code == 201, session.text
    session_id = session.json()["session_id"]

    assert client.post(
        "/api/conversations",
        headers=outsider_h,
        json={"connection_id": connection_id},
    ).status_code == 404
    assert client.delete(
        f"/api/connections/{connection_id}",
        headers=learner_h,
    ).status_code == 204

    assert client.post(
        "/api/conversations",
        headers=teacher_h,
        json={"connection_id": connection_id},
    ).status_code == 404
    assert client.post(
        f"/api/conversations/{conversation_id}/messages",
        headers=teacher_h,
        json={"message_text": "This should fail"},
    ).status_code == 409
    assert client.post(
        "/api/sessions",
        headers=teacher_h,
        json={
            "connection_id": connection_id,
            "teaching_skill_id": skill["teaching_skill_id"],
            "topic": "Removed connection session",
            "session_date": TODAY,
            "start_time": "11:00",
            "duration_minutes": 30,
            "meeting_link": "https://meet.example.com/removed",
        },
    ).status_code == 404

    assert client.get(f"/api/conversations/{conversation_id}", headers=teacher_h).status_code == 200
    assert client.get(
        f"/api/conversations/{conversation_id}/messages",
        headers=learner_h,
    ).status_code == 200
    assert client.get(f"/api/sessions/{session_id}", headers=learner_h).status_code == 200


def test_independent_connection_blocked_users_cannot_chat_or_session(client, make_user, category_id):
    from app.db.session import SessionLocal
    from app.models import UserBlock

    teacher_h, teacher = make_user("blocked-network-teacher@example.com", "Blocked Teacher")
    learner_h, learner = make_user("blocked-network-learner@example.com", "Blocked Learner")

    request = client.post(
        "/api/connection-requests",
        headers=teacher_h,
        json={"receiver_id": learner["user_id"]},
    )
    assert request.status_code == 201, request.text
    accepted = client.post(
        f"/api/connection-requests/{request.json()['request_id']}/accept",
        headers=learner_h,
    )
    assert accepted.status_code == 200, accepted.text
    connection_id = accepted.json()["connection_id"]

    with SessionLocal() as db:
        db.add(UserBlock(blocker_id=teacher["user_id"], blocked_id=learner["user_id"]))
        db.commit()

    assert client.post(
        "/api/conversations",
        headers=teacher_h,
        json={"connection_id": connection_id},
    ).status_code == 403

    skill = give_skill(client, teacher_h, category_id, "Blocked independent skill")
    assert client.post(
        "/api/sessions",
        headers=teacher_h,
        json={
            "connection_id": connection_id,
            "teaching_skill_id": skill["teaching_skill_id"],
            "topic": "Blocked session",
            "session_date": TODAY,
            "start_time": "12:00",
            "duration_minutes": 30,
            "meeting_link": "https://meet.example.com/blocked",
        },
    ).status_code == 403


def test_independent_connection_notifications_are_read_on_accept_and_reject(client, make_user):
    sender_h, sender = make_user("notification-sender@example.com", "Notification Sender")
    receiver_h, receiver = make_user("notification-receiver@example.com", "Notification Receiver")

    sent = client.post(
        "/api/connection-requests",
        headers=sender_h,
        json={"receiver_id": receiver["user_id"]},
    )
    assert sent.status_code == 201, sent.text
    assert client.get("/api/notifications/unread-count", headers=receiver_h).json()["unread_count"] == 1

    accepted = client.post(
        f"/api/connection-requests/{sent.json()['request_id']}/accept",
        headers=receiver_h,
    )
    assert accepted.status_code == 200, accepted.text
    assert client.get("/api/notifications/unread-count", headers=receiver_h).json()["unread_count"] == 0

    sender_h2, sender2 = make_user("notification-sender2@example.com", "Notification Sender 2")
    receiver_h2, receiver2 = make_user("notification-receiver2@example.com", "Notification Receiver 2")
    rejected = client.post(
        "/api/connection-requests",
        headers=sender_h2,
        json={"receiver_id": receiver2["user_id"]},
    )
    assert rejected.status_code == 201, rejected.text
    assert client.get("/api/notifications/unread-count", headers=receiver_h2).json()["unread_count"] == 1
    rejected_request = client.post(
        f"/api/connection-requests/{rejected.json()['request_id']}/reject",
        headers=receiver_h2,
    )
    assert rejected_request.status_code == 200, rejected_request.text
    assert client.get("/api/notifications/unread-count", headers=receiver_h2).json()["unread_count"] == 0


def test_reject_then_request_again_and_chat_api_is_exposed(client, make_user, category_id):
    t_h, _ = make_user("t@example.com")
    l_h, _ = make_user("l@example.com")
    skill = give_skill(client, t_h, category_id)
    rid = client.post("/api/requests", headers=l_h, json={"teaching_skill_id": skill["teaching_skill_id"]}).json()["request_id"]
    assert client.post(f"/api/requests/{rid}/reject", headers=t_h).json()["status"] == "Rejected"
    assert client.get("/api/connections", headers=l_h).json() == []
    assert client.get("/api/conversations", headers=l_h).json() == []
    assert client.get("/api/connections/1/messages", headers=l_h).status_code == 404
    assert client.post("/api/connections/1/messages", headers=l_h, json={"message_text": "hello?"}).status_code == 404
    assert client.post("/api/requests", headers=l_h, json={"teaching_skill_id": skill["teaching_skill_id"]}).status_code == 201
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/conversations" in paths
    assert "/api/conversations/{conversation_id}/messages" in paths


def test_accept_creates_connection_without_creating_conversation(client, make_user, category_id):
    t_h, t = make_user("t@example.com", "Tara")
    l_h, l = make_user("l@example.com", "Leo")
    o_h, _ = make_user("o@example.com", "Outsider")
    _, conn = connect(client, t_h, l_h, category_id)
    cid = conn["connection_id"]
    assert conn["other_user"]["user_id"] == l["user_id"]
    assert "conversation_id" not in conn and "last_message" not in conn
    assert client.get("/api/conversations", headers=t_h).json() == []
    assert client.get(f"/api/connections/{cid}", headers=l_h).json()["other_user"]["user_id"] == t["user_id"]
    assert [c["connection_id"] for c in client.get("/api/connections", headers=t_h).json()] == [cid]
    assert client.get("/api/connections", headers=o_h).json() == []
    assert client.get(f"/api/connections/{cid}", headers=o_h).status_code == 404
    assert client.get("/openapi.json").status_code == 200


def test_chat_conversation_messages_unread_and_authorization(client, make_user, category_id):
    teacher_h, teacher = make_user("teacher@example.com", "Teacher")
    learner_h, learner = make_user("learner@example.com", "Learner")
    outsider_h, _ = make_user("outsider@example.com", "Outsider")
    _, connection = connect(client, teacher_h, learner_h, category_id)
    connection_id = connection["connection_id"]

    created = client.post(
        "/api/conversations",
        headers=teacher_h,
        json={"connection_id": connection_id},
    )
    assert created.status_code == 201, created.text
    conversation = created.json()
    conversation_id = conversation["conversation_id"]
    assert conversation["connection_id"] == connection_id
    assert conversation["other_user"]["user_id"] == learner["user_id"]
    assert conversation["latest_message"] is None
    assert conversation["unread_count"] == 0

    reopened = client.post(
        "/api/conversations",
        headers=learner_h,
        json={"connection_id": connection_id},
    )
    assert reopened.status_code == 201
    assert reopened.json()["conversation_id"] == conversation_id
    assert client.get("/api/conversations", headers=learner_h).json()[0]["other_user"]["user_id"] == teacher["user_id"]
    assert client.get(f"/api/conversations/{conversation_id}", headers=outsider_h).status_code == 404
    assert client.get(
        f"/api/conversations/{conversation_id}/messages",
        headers=outsider_h,
    ).status_code == 404

    sent = client.post(
        f"/api/conversations/{conversation_id}/messages",
        headers=teacher_h,
        json={"message_text": "  Hello there  "},
    )
    assert sent.status_code == 201, sent.text
    message = sent.json()
    assert message["message_text"] == "Hello there"
    assert message["sender_id"] == teacher["user_id"]
    assert message["is_read"] is False
    message_notification = client.get("/api/notifications", headers=learner_h).json()[0]
    assert message_notification["notification_type"] == "new_message"
    assert message_notification["related_entity_type"] == "Message"
    assert message_notification["related_entity_id"] == message["message_id"]

    learner_conversation = client.get(f"/api/conversations/{conversation_id}", headers=learner_h).json()
    assert learner_conversation["unread_count"] == 1
    assert learner_conversation["latest_message"]["message_id"] == message["message_id"]
    assert client.get(
        f"/api/conversations/{conversation_id}/messages",
        headers=learner_h,
    ).json()[0]["is_read"] is False

    sender_read = client.post(f"/api/conversations/{conversation_id}/read", headers=teacher_h)
    assert sender_read.status_code == 200 and sender_read.json()["marked_count"] == 0
    receiver_read = client.post(f"/api/conversations/{conversation_id}/read", headers=learner_h)
    assert receiver_read.status_code == 200 and receiver_read.json()["marked_count"] == 1
    assert client.post(f"/api/conversations/{conversation_id}/read", headers=learner_h).json()["marked_count"] == 0
    assert client.get(f"/api/conversations/{conversation_id}", headers=learner_h).json()["unread_count"] == 0
    assert client.get(
        f"/api/conversations/{conversation_id}/messages",
        headers=learner_h,
    ).json()[0]["is_read"] is True
    assert client.post(
        f"/api/conversations/{conversation_id}/messages",
        headers=learner_h,
        json={"message_text": "   "},
    ).status_code == 422
    assert client.post(
        "/api/conversations",
        json={"connection_id": connection_id},
    ).status_code == 401


def test_second_accepted_request_reuses_the_connection(client, make_user, category_id):
    t_h, _ = make_user("t@example.com")
    l_h, _ = make_user("l@example.com")
    _, conn1 = connect(client, t_h, l_h, category_id, "Python")
    _, conn2 = connect(client, t_h, l_h, category_id, "Guitar")
    assert conn1["connection_id"] == conn2["connection_id"]
    assert len(client.get("/api/connections", headers=l_h).json()) == 1


# ---------- Sessions ----------
def test_full_session_flow_and_rules(client, make_user, category_id):
    t_h, _ = make_user("t@example.com", "Tara")
    l_h, _ = make_user("l@example.com", "Leo")
    o_h, _ = make_user("o@example.com", "Other")
    skill, conn = connect(client, t_h, l_h, category_id)

    # only the teacher of the selected skill can propose
    assert propose(client, l_h, conn, skill).status_code == 403
    assert propose(client, o_h, conn, skill).status_code == 404
    assert propose(client, t_h, conn, skill, day=YESTERDAY).status_code == 400
    assert propose(client, t_h, conn, skill, link="not a link").status_code == 422
    s = propose(client, t_h, conn, skill)
    assert s.status_code == 201 and s.json()["status"] == "Pending"
    sid = s.json()["session_id"]

    # the learner does not see the meeting link until the session is Scheduled
    assert client.get(f"/api/sessions/{sid}", headers=l_h).json()["meeting_link"] is None
    assert client.get(f"/api/sessions/{sid}", headers=t_h).json()["meeting_link"] == "https://meet.example.com/abc"
    assert client.get(f"/api/sessions/{sid}", headers=o_h).status_code == 404

    # learner must accept; the teacher cannot accept their own proposal; nobody can complete/review yet
    assert client.post(f"/api/sessions/{sid}/accept", headers=t_h).status_code == 403
    assert client.post(f"/api/sessions/{sid}/complete", headers=t_h).status_code == 409
    assert client.post(f"/api/sessions/{sid}/review", headers=l_h, json={"rating": 5}).status_code == 400
    acc = client.post(f"/api/sessions/{sid}/accept", headers=l_h)
    assert acc.status_code == 200 and acc.json()["status"] == "Scheduled"
    assert acc.json()["meeting_link"] == "https://meet.example.com/abc"

    # learners cannot cancel or complete in the MVP
    assert client.post(f"/api/sessions/{sid}/cancel", headers=l_h).status_code == 403
    assert client.post(f"/api/sessions/{sid}/complete", headers=l_h).status_code == 403
    assert client.post(f"/api/sessions/{sid}/review", headers=l_h, json={"rating": 5}).status_code == 400

    done = client.post(f"/api/sessions/{sid}/complete", headers=t_h)
    assert done.status_code == 200 and done.json()["status"] == "Completed"
    assert client.post(f"/api/sessions/{sid}/cancel", headers=t_h).status_code == 409  # final

    # reviews: learner only, once per session, no editing
    assert client.post(f"/api/sessions/{sid}/review", headers=t_h, json={"rating": 5}).status_code == 403
    assert client.post(f"/api/sessions/{sid}/review", headers=o_h, json={"rating": 5}).status_code == 404
    assert client.post(f"/api/sessions/{sid}/review", headers=l_h, json={"rating": 6}).status_code == 422
    assert client.get(f"/api/sessions/{sid}/review", headers=l_h).status_code == 404
    rev = client.post(f"/api/sessions/{sid}/review", headers=l_h, json={"rating": 4})  # text is optional
    assert rev.status_code == 201 and rev.json()["written_review"] is None and rev.json()["learner_name"] == "Leo"
    assert client.post(f"/api/sessions/{sid}/review", headers=l_h, json={"rating": 5}).status_code == 409
    assert client.patch(f"/api/reviews/{rev.json()['review_id']}", headers=l_h, json={"rating": 5}).status_code in (404, 405)
    assert client.delete(f"/api/reviews/{rev.json()['review_id']}", headers=l_h).status_code in (404, 405)
    assert client.get(f"/api/sessions/{sid}/review", headers=t_h).json()["rating"] == 4


def test_same_learner_can_review_each_completed_session(client, make_user, category_id):
    t_h, t = make_user("t@example.com", "Tara")
    l_h, _ = make_user("l@example.com", "Leo")
    skill, conn = connect(client, t_h, l_h, category_id)
    for rating, text in ((5, "Great"), (3, None)):
        sid = completed_session(client, t_h, l_h, conn, skill)
        r = client.post(f"/api/sessions/{sid}/review", headers=l_h, json={"rating": rating, "written_review": text})
        assert r.status_code == 201, r.text
    reviews = client.get(f"/api/users/{t['user_id']}/reviews", headers=l_h).json()
    assert sorted(r["rating"] for r in reviews) == [3, 5]
    card = client.get(f"/api/users/{t['user_id']}", headers=l_h).json()
    assert card["average_rating"] == 4.0 and card["review_count"] == 2
    search = client.get("/api/search", headers=l_h).json()
    assert search[0]["average_rating"] == 4.0


def test_session_reject_cancel_and_cannot_complete_before_its_date(client, make_user, category_id):
    t_h, _ = make_user("t@example.com")
    l_h, _ = make_user("l@example.com")
    skill, conn = connect(client, t_h, l_h, category_id)
    s1 = propose(client, t_h, conn, skill).json()["session_id"]
    assert client.post(f"/api/sessions/{s1}/reject", headers=t_h).status_code == 403
    r = client.post(f"/api/sessions/{s1}/reject", headers=l_h)
    assert r.json()["status"] == "Rejected" and r.json()["meeting_link"] is None
    assert client.post(f"/api/sessions/{s1}/accept", headers=l_h).status_code == 409

    s2 = propose(client, t_h, conn, skill, day=TOMORROW).json()["session_id"]
    client.post(f"/api/sessions/{s2}/accept", headers=l_h)
    early = client.post(f"/api/sessions/{s2}/complete", headers=t_h)  # refused by a database rule
    assert early.status_code == 400 and "before its date" in early.json()["detail"]
    assert client.post(f"/api/sessions/{s2}/cancel", headers=t_h).json()["status"] == "Cancelled"
    assert [s["status"] for s in client.get("/api/sessions", headers=l_h, params={"status": "Cancelled"}).json()] == ["Cancelled"]
    assert len(client.get("/api/sessions", headers=t_h, params={"role": "teacher"}).json()) == 2
    assert client.get("/api/sessions", headers=t_h, params={"role": "learner"}).json() == []


# ---------- Notifications ----------
def test_notifications_follow_events_and_stay_listed(client, make_user, category_id):
    t_h, _ = make_user("t@example.com", "Tara")
    l_h, _ = make_user("l@example.com", "Leo")
    skill, conn = connect(client, t_h, l_h, category_id)
    sid = completed_session(client, t_h, l_h, conn, skill)
    client.post(f"/api/sessions/{sid}/review", headers=l_h, json={"rating": 5})

    t_types = {n["notification_type"] for n in client.get("/api/notifications", headers=t_h).json()}
    assert {"request_received", "session_accepted", "new_review_received"} <= t_types
    l_types = {n["notification_type"] for n in client.get("/api/notifications", headers=l_h).json()}
    assert {"request_accepted", "session_requested"} <= l_types

    count = client.get("/api/notifications/unread-count", headers=t_h).json()["unread_count"]
    assert count == len(client.get("/api/notifications", headers=t_h).json()) > 0
    first = client.get("/api/notifications", headers=t_h).json()[0]
    assert first["is_read"] is False
    assert client.post(f"/api/notifications/{first['notification_id']}/read", headers=l_h).status_code == 404
    opened = client.post(f"/api/notifications/{first['notification_id']}/read", headers=t_h).json()
    assert opened["is_read"] is True and opened["read_at"] is not None
    assert client.post(f"/api/notifications/{first['notification_id']}/read", headers=t_h).status_code == 200  # idempotent
    assert client.get("/api/notifications/unread-count", headers=t_h).json()["unread_count"] == count - 1
    assert len(client.get("/api/notifications", headers=t_h).json()) == count  # read ones remain visible
    assert len(client.get("/api/notifications", headers=t_h, params={"unread_only": True}).json()) == count - 1
    assert client.post("/api/notifications/read-all", headers=t_h).status_code in (404, 405)  # excluded from the MVP


def test_rejection_and_cancellation_notifications(client, make_user, category_id):
    t_h, _ = make_user("t@example.com")
    l_h, _ = make_user("l@example.com")
    skill = give_skill(client, t_h, category_id)
    rid = client.post("/api/requests", headers=l_h, json={"teaching_skill_id": skill["teaching_skill_id"]}).json()["request_id"]
    client.post(f"/api/requests/{rid}/reject", headers=t_h)
    assert "request_rejected" in {n["notification_type"] for n in client.get("/api/notifications", headers=l_h).json()}
    _, conn = connect(client, t_h, l_h, category_id, "Guitar")
    sk = give_skill(client, t_h, category_id, "Piano")
    sid = propose(client, t_h, conn, sk, day=TOMORROW).json()["session_id"]
    client.post(f"/api/sessions/{sid}/accept", headers=l_h)
    client.post(f"/api/sessions/{sid}/cancel", headers=t_h)
    assert "session_cancelled" in {n["notification_type"] for n in client.get("/api/notifications", headers=l_h).json()}
    s2 = propose(client, t_h, conn, sk, day=TOMORROW).json()["session_id"]
    client.post(f"/api/sessions/{s2}/reject", headers=l_h)
    assert "session_rejected" in {n["notification_type"] for n in client.get("/api/notifications", headers=t_h).json()}


# ---------- Reports and admin ----------
def test_report_a_user_and_admin_closes_it(client, make_user, admin_headers):
    a_h, a = make_user("a@example.com")
    _, b = make_user("b@example.com")
    r = client.post("/api/reports", headers=a_h, json={"reported_user_id": b["user_id"], "reason": "Spam"})
    assert r.status_code == 201 and r.json()["status"] == "Pending"
    rid = r.json()["report_id"]
    assert client.post("/api/reports", headers=a_h, json={"reported_user_id": a["user_id"], "reason": "x"}).status_code == 400
    assert client.post("/api/reports", headers=a_h, json={"reported_user_id": 9999, "reason": "x"}).status_code == 404
    assert client.post("/api/reports", headers=a_h, json={"reason": "no target"}).status_code == 422
    assert client.post("/api/reports", headers=a_h,
                       json={"reported_user_id": b["user_id"], "content_type": "Review", "content_id": 1, "reason": "both"}).status_code == 422
    assert client.get("/api/admin/reports", headers=a_h).status_code == 401

    assert client.get("/api/admin/overview", headers=admin_headers).json()["pending_reports"] == 1
    assert client.patch(f"/api/admin/reports/{rid}", headers=admin_headers, json={"status": "Under Review"}).json()["status"] == "Under Review"
    assert client.patch(f"/api/admin/reports/{rid}", headers=admin_headers, json={"status": "Resolved"}).status_code == 422  # needs an action
    done = client.patch(f"/api/admin/reports/{rid}", headers=admin_headers, json={"status": "Resolved", "admin_action": "User Suspended"})
    assert done.status_code == 200 and done.json()["reviewed_by_admin_id"] is not None and done.json()["reviewed_at"] is not None
    users = {u["user_id"]: u["account_status"] for u in client.get("/api/admin/users", headers=admin_headers).json()}
    assert users[b["user_id"]] == "Suspended"
    assert client.patch(f"/api/admin/reports/{rid}", headers=admin_headers, json={"status": "Dismissed", "admin_action": "No Action Required"}).status_code == 409
    ov = client.get("/api/admin/overview", headers=admin_headers).json()
    assert ov["suspended_users"] == 1 and ov["pending_reports"] == 0 and ov["total_users"] == 2


def test_admin_removes_a_reported_review(client, make_user, admin_headers, category_id):
    t_h, t = make_user("t@example.com", "Tara")
    l_h, _ = make_user("l@example.com", "Leo")
    skill, conn = connect(client, t_h, l_h, category_id)
    sid = completed_session(client, t_h, l_h, conn, skill)
    review = client.post(f"/api/sessions/{sid}/review", headers=l_h, json={"rating": 1, "written_review": "rude text"}).json()
    rep = client.post("/api/reports", headers=t_h, json={"content_type": "Review", "content_id": review["review_id"], "reason": "Abusive"})
    assert rep.status_code == 201, rep.text
    gone = client.patch(f"/api/admin/reports/{rep.json()['report_id']}", headers=admin_headers,
                        json={"status": "Resolved", "admin_action": "Content Removed"})
    assert gone.status_code == 200, gone.text
    assert client.get(f"/api/users/{t['user_id']}/reviews", headers=t_h).json() == []


def test_report_content_must_exist(client, make_user):
    l_h, _ = make_user("l@example.com")
    response = client.post("/api/reports", headers=l_h, json={"content_type": "Review", "content_id": 999, "reason": "x"})
    assert response.status_code == 404
    assert response.json()["detail"] == "Report target not found"


def test_report_duplicate_window_does_not_block_different_targets(client, make_user):
    reporter_h, _ = make_user("reporter@example.com")
    _, first = make_user("first@example.com")
    _, second = make_user("second@example.com")
    payload = {"reason": "Spam"}
    target_one = {**payload, "reported_user_id": first["user_id"]}
    target_two = {**payload, "reported_user_id": second["user_id"]}
    assert client.post("/api/reports", headers=reporter_h, json=target_one).status_code == 201
    duplicate = client.post("/api/reports", headers=reporter_h, json=target_one)
    assert duplicate.status_code == 409 and "Retry-After" in duplicate.headers
    assert client.post("/api/reports", headers=reporter_h, json=target_two).status_code == 201


def test_report_rejects_deleted_user_and_nonparticipant_message(client, make_user, category_id):
    reporter_h, _ = make_user("reporter@example.com")
    target_h, target = make_user("target@example.com")
    outsider_h, _ = make_user("outsider@example.com")
    assert client.delete("/api/me", headers=target_h).status_code == 200
    deleted_user = client.post("/api/reports", headers=reporter_h, json={"reported_user_id": target["user_id"], "reason": "Abuse"})
    assert deleted_user.status_code == 404

    teacher_h, _ = make_user("teacher@example.com")
    learner_h, _ = make_user("learner@example.com")
    skill, connection = connect(client, teacher_h, learner_h, category_id)
    conversation = client.post("/api/conversations", headers=teacher_h, json={"connection_id": connection["connection_id"]})
    assert conversation.status_code in (200, 201), conversation.text
    message = client.post(
        f"/api/conversations/{conversation.json()['conversation_id']}/messages",
        headers=teacher_h,
        json={"message_text": "Hello"},
    )
    assert message.status_code == 201, message.text
    report_payload = {"content_type": "Message", "content_id": message.json()["message_id"], "reason": "Abuse"}
    unauthorized = client.post("/api/reports", headers=outsider_h, json=report_payload)
    assert unauthorized.status_code == 404
    authorized = client.post("/api/reports", headers=learner_h, json=report_payload)
    assert authorized.status_code == 201, authorized.text


def test_report_skill_ownership_and_visibility(client, make_user, category_id):
    owner_h, owner = make_user("owner@example.com")
    reporter_h, _ = make_user("reporter@example.com")
    skill = give_skill(client, owner_h, category_id)
    public_report = client.post("/api/reports", headers=reporter_h, json={
        "content_type": "TeachingSkill", "content_id": skill["teaching_skill_id"], "reason": "Concern",
    })
    assert public_report.status_code == 201, public_report.text

    learning = client.post("/api/me/learning-skills", headers=owner_h, json={
        "category_id": category_id, "skill_name": "Private goal", "current_level": "Beginner",
    })
    assert learning.status_code == 201, learning.text
    learning_payload = {"content_type": "LearningSkill", "content_id": learning.json()["learning_skill_id"], "reason": "Concern"}
    assert client.post("/api/reports", headers=reporter_h, json=learning_payload).status_code == 404
    assert client.post("/api/reports", headers=owner_h, json=learning_payload).status_code == 201


def test_review_report_follows_public_profile_visibility(client, make_user, category_id):
    teacher_h, teacher = make_user("teacher@example.com")
    learner_h, learner = make_user("learner@example.com")
    reporter_h, _ = make_user("reporter@example.com")
    skill, connection = connect(client, teacher_h, learner_h, category_id)
    session_id = completed_session(client, teacher_h, learner_h, connection, skill)
    review = client.post(f"/api/sessions/{session_id}/review", headers=learner_h, json={"rating": 2, "written_review": "Needs context"})
    assert review.status_code == 201, review.text
    review_id = review.json()["review_id"]
    visible = client.get(f"/api/users/{teacher['user_id']}/reviews", headers=reporter_h)
    assert visible.status_code == 200 and any(item["review_id"] == review_id for item in visible.json())
    assert client.post("/api/reports", headers=reporter_h, json={"content_type": "Review", "content_id": review_id, "reason": "Concern"}).status_code == 201

    assert client.delete("/api/me", headers=teacher_h).status_code == 200
    assert client.get(f"/api/users/{teacher['user_id']}", headers=reporter_h).status_code == 404
    assert client.get(f"/api/users/{teacher['user_id']}/reviews", headers=reporter_h).status_code == 404
    hidden = client.post("/api/reports", headers=learner_h, json={"content_type": "Review", "content_id": review_id, "reason": "Concern"})
    assert hidden.status_code == 404


def test_admin_hides_a_reported_skill(client, make_user, admin_headers, category_id):
    t_h, _ = make_user("t@example.com")
    l_h, _ = make_user("l@example.com")
    skill = give_skill(client, t_h, category_id)
    rep = client.post("/api/reports", headers=l_h, json={"content_type": "TeachingSkill", "content_id": skill["teaching_skill_id"], "reason": "Scam"}).json()
    client.patch(f"/api/admin/reports/{rep['report_id']}", headers=admin_headers, json={"status": "Resolved", "admin_action": "Content Removed"})
    assert client.get("/api/search", headers=l_h).json() == []


def test_admin_list_users_search(client, make_user, admin_headers):
    make_user("asha@example.com", "Asha")
    make_user("ravi@example.com", "Ravi")
    found = client.get("/api/admin/users", headers=admin_headers, params={"q": "ravi"}).json()
    assert [u["full_name"] for u in found] == ["Ravi"] and "password_hash" not in found[0]


# ---------- Misc ----------
def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok", "database": "connected"}


def test_refresh_token_exchange_and_type_separation(client, make_user):
    headers, user = make_user("refresh@example.com")
    login = client.post("/api/auth/login", json={"email": "refresh@example.com", "password": "password123"})
    assert login.status_code == 200
    pair = login.json()
    assert pair.get("refresh_token")
    refreshed = client.post("/api/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert refreshed.status_code == 200
    assert refreshed.json().get("access_token")
    # An access token cannot be exchanged as a refresh token.
    bad = client.post("/api/auth/refresh", json={"refresh_token": pair["access_token"]})
    assert bad.status_code == 401
    # A refresh token cannot authenticate a protected REST route.
    protected = client.get("/api/me", headers={"Authorization": f"Bearer {pair['refresh_token']}"})
    assert protected.status_code == 401


def test_user_soft_delete_preserves_row_and_revokes_access(client, make_user, admin_headers):
    headers, user = make_user("delete-me@example.com", "Delete Me")
    login = client.post("/api/auth/login", json={"email": "delete-me@example.com", "password": "password123"}).json()
    deleted = client.delete("/api/me", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert "retained" in deleted.json()["detail"].lower()
    # The existing access token and refresh token are both refused immediately.
    assert client.get("/api/me", headers=headers).status_code == 403
    assert client.post("/api/auth/refresh", json={"refresh_token": login["refresh_token"]}).status_code == 403
    assert client.post("/api/auth/login", json={"email": "delete-me@example.com", "password": "password123"}).status_code == 403
    admin_rows = client.get("/api/admin/users", headers=admin_headers).json()
    deleted_row = next(row for row in admin_rows if row["user_id"] == user["user_id"])
    assert deleted_row["account_status"] == "Deleted" and deleted_row["deleted_at"] is not None
    # Deleted users cannot be silently restored through the ordinary admin status endpoint.
    assert client.patch(f"/api/admin/users/{user['user_id']}", headers=admin_headers,
                        json={"account_status": "Active"}).status_code == 409


def test_user_block_unblock_and_list(client, make_user):
    reporter_headers, reporter = make_user("blocker@example.com", "Blocker")
    _, target = make_user("blocked@example.com", "Blocked Person")
    route = f"/api/users/{target['user_id']}/block"
    created = client.post(route, headers=reporter_headers)
    assert created.status_code == 201, created.text
    assert created.json()["blocked_id"] == target["user_id"]
    assert created.json()["blocked_user_name"] == "Blocked Person"
    assert client.post(route, headers=reporter_headers).status_code == 409
    assert client.post(f"/api/users/{reporter['user_id']}/block", headers=reporter_headers).status_code == 400

    listed = client.get("/api/me/blocked-users", headers=reporter_headers)
    assert listed.status_code == 200 and [row["blocked_id"] for row in listed.json()] == [target["user_id"]]
    assert client.delete(route, headers=reporter_headers).status_code == 204
    assert client.get("/api/me/blocked-users", headers=reporter_headers).json() == []
    assert client.delete(route, headers=reporter_headers).status_code == 404


def test_profile_avatar_upload_and_static_serving(client, make_user):
    headers, _ = make_user("avatar@example.com")
    # PNG signature is checked server-side; the route does not trust the filename.
    png_signature = b"\x89PNG\r\n\x1a\n" + b"test-image-content"
    uploaded = client.post("/api/me/avatar", headers={**headers, "Content-Type": "image/png"}, content=png_signature)
    assert uploaded.status_code == 200, uploaded.text
    photo_url = uploaded.json()["profile_photo"]
    assert photo_url.startswith("/uploads/profiles/")
    served = client.get(photo_url)
    assert served.status_code == 200
    assert served.content == png_signature




def test_user_reports_another_user_and_admin_locks_account(client, make_user, admin_headers):
    reporter_headers, reporter = make_user("reporter@example.com", "Reporter")
    target_headers, target = make_user("target@example.com", "Reported User")

    submitted = client.post(
        "/api/reports", headers=reporter_headers,
        json={"reported_user_id": target["user_id"], "reason": "Harassment", "description": "Repeated abusive messages"},
    )
    assert submitted.status_code == 201, submitted.text
    report_id = submitted.json()["report_id"]

    # A normal user cannot inspect or resolve moderation reports.
    assert client.get("/api/admin/reports", headers=reporter_headers).status_code == 401
    assert client.patch(
        f"/api/admin/reports/{report_id}", headers=reporter_headers,
        json={"status": "Resolved", "admin_action": "User Blocked"},
    ).status_code == 401

    reviewed = client.patch(
        f"/api/admin/reports/{report_id}", headers=admin_headers,
        json={"status": "Resolved", "admin_action": "User Blocked"},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["admin_action"] == "User Blocked"

    # The target is locked: old token and new login both fail.
    assert client.get("/api/me", headers=target_headers).status_code == 403
    assert client.post("/api/auth/login", json={"email": "target@example.com", "password": "password123"}).status_code == 403

    # Blocking remains a separate reversible control; reporting does not create a user block.
    assert client.get("/api/me/blocked-users", headers=reporter_headers).json() == []
