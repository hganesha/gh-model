package com.shop.catalog

import jakarta.persistence.Entity
import jakarta.persistence.Id
import jakarta.persistence.Table

@Entity
@Table(name = "products")
class Product(@Id val id: Long = 0, val name: String = "")
