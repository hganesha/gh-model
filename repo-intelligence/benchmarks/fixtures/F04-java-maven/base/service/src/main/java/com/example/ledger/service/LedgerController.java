package com.example.ledger.service;

import com.example.ledger.core.Entry;
import org.springframework.web.bind.annotation.*;

@RestController
public class LedgerController {
    @GetMapping("/entries")
    public java.util.List<Entry> entries() { return java.util.List.of(); }
}
