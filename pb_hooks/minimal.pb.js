// minimal.pb.js — if this route works, hooks DO load and the issue is in main.pb.js
routerAdd("GET", "/api/creator/mini-ping", function(e) {
    return e.json(200, { ok: true, hooks: "loaded" });
});
