package com.shop.catalog

import org.springframework.graphql.data.method.annotation.Argument
import org.springframework.graphql.data.method.annotation.MutationMapping
import org.springframework.graphql.data.method.annotation.QueryMapping
import org.springframework.web.bind.annotation.*

@RestController
@RequestMapping("/api/products")
class CatalogController(private val products: ProductRepository) {
    @GetMapping("/{id}")
    fun get(@PathVariable id: Long): Product = products.findById(id).orElseThrow()

    @QueryMapping
    fun products(@Argument first: Int?): List<Product> = products.findAll()

    @QueryMapping
    fun product(@Argument id: Long): Product? = products.findById(id).orElse(null)

    @MutationMapping
    fun addProduct(@Argument name: String): Product = products.save(Product(name = name))
}
