-- CreateTable
CREATE TABLE "orders" (
    "id" SERIAL NOT NULL,
    "sku" TEXT NOT NULL,
    "quantity" INTEGER NOT NULL,

    CONSTRAINT "orders_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "orders_staging" (
    "id" SERIAL NOT NULL,
    "payload" JSONB NOT NULL
);
