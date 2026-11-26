/**
 * Order desk application settings and request lifecycle.
 */
component {
    this.name = "OrderDesk";
    this.datasource = "orderdb";
    this.sessionManagement = true;
    this.sessionTimeout = createTimeSpan(0, 0, 30, 0);
    this.loginStorage = "session";
    this.mappings["/lib"] = expandPath("./lib");

    public boolean function onApplicationStart() {
        application.started = now();
        application.currency = "EUR";
        return true;
    }

    public void function onSessionStart() {
        session.cart = [];
    }

    public boolean function onRequestStart(required string targetPage) {
        if (!isUserLoggedIn() && !findNoCase("login.cfm", arguments.targetPage)) {
            location(url = "login.cfm", addToken = false);
        }
        request.startedAt = getTickCount();
        return true;
    }

    public void function onError(any exception, string eventName) {
        writeLog(file = "orderdesk", text = "#arguments.eventName#: #arguments.exception.message#");
        include "error.cfm";
    }
}
