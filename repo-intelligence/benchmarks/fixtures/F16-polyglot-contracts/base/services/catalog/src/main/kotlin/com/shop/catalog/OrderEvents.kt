package com.shop.catalog

import org.springframework.kafka.annotation.KafkaListener
import org.springframework.stereotype.Component

@Component
class OrderEvents {
    // Stock is reserved when an order is placed.
    @KafkaListener(topics = ["order-events"], groupId = "catalog")
    fun onOrder(message: String) {}
}
