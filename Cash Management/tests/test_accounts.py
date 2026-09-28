def test_create_and_list_accounts(client):
    resp = client.post("/api/v1/accounts", json={"code": "1000", "name": "Cash", "type": "asset"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["code"] == "1000"
    assert body["is_active"] is True

    client.post("/api/v1/accounts", json={"code": "4000", "name": "Sales", "type": "revenue"})
    codes = [a["code"] for a in client.get("/api/v1/accounts").json()]
    assert codes == ["1000", "4000"]


def test_duplicate_code_rejected(client):
    payload = {"code": "1000", "name": "Cash", "type": "asset"}
    assert client.post("/api/v1/accounts", json=payload).status_code == 201
    assert client.post("/api/v1/accounts", json=payload).status_code == 409


def test_invalid_type_rejected(client):
    resp = client.post("/api/v1/accounts", json={"code": "9", "name": "X", "type": "bogus"})
    assert resp.status_code == 422


def test_get_missing_account(client):
    assert client.get("/api/v1/accounts/999").status_code == 404


def test_deactivate_and_activate_account(client):
    account = client.post(
        "/api/v1/accounts", json={"code": "1000", "name": "Cash", "type": "asset"}
    ).json()

    resp = client.post(f"/api/v1/accounts/{account['id']}/deactivate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False
    assert client.get(f"/api/v1/accounts/{account['id']}").json()["is_active"] is False

    resp = client.post(f"/api/v1/accounts/{account['id']}/activate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True

    # Idempotent.
    assert client.post(f"/api/v1/accounts/{account['id']}/activate").json()["is_active"] is True


def test_activate_missing_account(client):
    assert client.post("/api/v1/accounts/999/activate").status_code == 404
    assert client.post("/api/v1/accounts/999/deactivate").status_code == 404
