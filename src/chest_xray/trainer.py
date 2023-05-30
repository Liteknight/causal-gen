import os
import copy
import torch
import torch.nn as nn
from tqdm import tqdm

from utils import linear_warmup, write_images


def trainer(args, model, ema, dataloaders, optimizer, scheduler, writer, logger):
    for k in sorted(vars(args)):
        logger.info(f"--{k}={vars(args)[k]}")
    logger.info(f"total params: {sum(p.numel() for p in model.parameters()):,}")

    def run_epoch(dataloader, training=True):
        model.train(training)
        model.zero_grad(set_to_none=True)
        stats = {k: 0 for k in ["elbo", "nll", "kl", "n"]}
        updates_skipped = 0

        mininterval = 300 if "SLURM_JOB_ID" in os.environ else 0.1
        loader = tqdm(
            enumerate(dataloader), total=len(dataloader), mininterval=mininterval
        )

        for i, batch in loader:
            # [-1, 1] image preprocessing in Dataloader
            batch["x"] = batch["x"].cuda().float()
            batch["pa"] = (
                batch["pa"][..., None, None]
                .repeat(1, 1, args.input_res, args.input_res)
                .cuda()
                .float()
            )
            update_stats = True

            if training:
                args.iter = i + (args.epoch - 1) * len(dataloader)
                if args.beta_warmup_steps > 0:
                    args.beta = args.beta_target * linear_warmup(
                        args.beta_warmup_steps
                    )(args.iter + 1)
                    writer.add_scalar("train/beta_kl", args.beta, args.iter)

                out = model(batch["x"], batch["pa"], beta=args.beta)
                out["elbo"] = out["elbo"] / args.accu_steps
                out["elbo"].backward()

                if i % args.accu_steps == 0:  # gradient accumulation update
                    grad_norm = nn.utils.clip_grad_norm_(
                        model.parameters(), args.grad_clip
                    )
                    writer.add_scalar("train/grad_norm", grad_norm, args.iter)
                    nll_nan = torch.isnan(out["nll"]).sum()
                    kl_nan = torch.isnan(out["kl"]).sum()

                    if grad_norm < args.grad_skip and nll_nan == 0 and kl_nan == 0:
                        optimizer.step()
                        scheduler.step()
                        ema.update()
                    else:
                        updates_skipped += 1
                        update_stats = False
                        logger.info(
                            f"Updates skipped: {updates_skipped}"
                            + f" - grad_norm: {grad_norm:.3f}"
                            + f" - nll_nan: {nll_nan.item()} - kl_nan: {kl_nan.item()}"
                        )
                    model.zero_grad(set_to_none=True)

                    if args.iter % args.viz_freq == 0 or (args.iter in early_evals):
                        with torch.no_grad():
                            write_images(args, ema.ema_model, viz_batch)
            else:
                with torch.no_grad():
                    out = ema.ema_model(batch["x"], batch["pa"], beta=args.beta)

            if update_stats:
                if training:
                    out["elbo"] *= args.accu_steps
            bs = batch["x"].shape[0]
            stats["n"] += bs  # samples seen counter
            stats["elbo"] += out["elbo"] * bs
            stats["nll"] += out["nll"] * bs
            stats["kl"] += out["kl"] * bs

            split = "train" if training else "valid"
            loader.set_description(
                f' => {split} | nelbo: {stats["elbo"] / stats["n"]:.3f}'
                + f' - nll: {stats["nll"] / stats["n"]:.3f}'
                + f' - kl: {stats["kl"] / stats["n"]:.3f}'
                + f" - lr: {scheduler.get_last_lr()[0]:.6g}"
                + (f" - grad norm: {grad_norm:.2f}" if training else ""),
                refresh=False,
            )
        return {k: v / stats["n"] for k, v in stats.items() if k != "n"}

    if args.beta_warmup_steps > 0:
        args.beta_target = copy.deepcopy(args.beta)

    viz_batch = next(iter(dataloaders["valid"]))
    n = args.bs
    viz_batch["x"] = viz_batch["x"][:n].cuda().float()  # [-1,1]
    viz_batch["pa"] = (
        viz_batch["pa"][:n, :, None, None]
        .repeat(1, 1, args.input_res, args.input_res)
        .cuda()
        .float()
    )
    early_evals = set([1] + [2**exp for exp in range(3, 14)])

    # Start training loop
    for epoch in range(args.start_epoch, args.epochs):
        args.epoch = epoch + 1
        logger.info(f"Epoch {args.epoch}:")

        stats = run_epoch(dataloaders["train"], training=True)

        writer.add_scalar(f"nelbo/train", stats["elbo"], args.epoch)
        writer.add_scalar(f"nll/train", stats["nll"], args.epoch)
        writer.add_scalar(f"kl/train", stats["kl"], args.epoch)
        logger.info(
            f'=> train | nelbo: {stats["elbo"]:.4f}'
            + f' - nll: {stats["nll"]:.4f} - kl: {stats["kl"]:.4f}'
            + f" - steps: {args.iter}"
        )

        if (args.epoch - 1) % args.eval_freq == 0:
            valid_stats = run_epoch(dataloaders["valid"], training=False)

            writer.add_scalar(f"nelbo/valid", valid_stats["elbo"], args.epoch)
            writer.add_scalar(f"nll/valid", valid_stats["nll"], args.epoch)
            writer.add_scalar(f"kl/valid", valid_stats["kl"], args.epoch)
            logger.info(
                f'=> valid | nelbo: {valid_stats["elbo"]:.4f}'
                + f' - nll: {valid_stats["nll"]:.4f} - kl: {valid_stats["kl"]:.4f}'
                + f" - steps: {args.iter}"
            )

            if valid_stats["elbo"] < args.best_loss:
                args.best_loss = valid_stats["elbo"]
                save_dict = {
                    "epoch": args.epoch,
                    "step": args.epoch * len(dataloaders["train"]),
                    "best_loss": args.best_loss,
                    "model_state_dict": model.state_dict(),
                    "ema_model_state_dict": ema.ema_model.state_dict(),
                    "optimizer_state_dict": optimizer.state_dict(),
                    "scheduler_state_dict": scheduler.state_dict(),
                    "hparams": vars(args),
                }
                ckpt_path = os.path.join(args.save_dir, "checkpoint.pt")
                torch.save(save_dict, ckpt_path)
                logger.info(f"Model saved: {ckpt_path}")
    return
