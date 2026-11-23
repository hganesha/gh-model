// Named "service" but exports helpers only; nothing listens on a port.
module.exports = { slugify: (s) => s.toLowerCase().replace(/\s+/g, "-") };
