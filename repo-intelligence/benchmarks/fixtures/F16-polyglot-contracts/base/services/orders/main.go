package main

import (
	"context"

	"github.com/segmentio/kafka-go"
	pb "example.com/shop/orders/gen/orders/v1"
)

const eventsTopic = "order-events"

type server struct {
	pb.UnimplementedOrdersServer
	events *kafka.Writer
}

func (s *server) GetOrder(ctx context.Context, in *pb.GetOrderRequest) (*pb.Order, error) {
	return &pb.Order{Id: in.Id}, nil
}

func newWriter() *kafka.Writer {
	return &kafka.Writer{Addr: kafka.TCP("kafka:9092"), Topic: eventsTopic}
}
