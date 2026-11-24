import * as aws from "@pulumi/aws";
import * as pulumi from "@pulumi/pulumi";

const config = new pulumi.Config();
export const assets = new aws.s3.Bucket("assets", { forceDestroy: false });
export const audit = new aws.sqs.Queue("audit", { messageRetentionSeconds: 1209600 });
