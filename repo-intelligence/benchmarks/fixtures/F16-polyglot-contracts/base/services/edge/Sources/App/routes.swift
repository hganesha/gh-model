import Vapor

func routes(_ app: Application) throws {
    let api = app.grouped("api")
    api.get("ping") { req in "pong" }
}
