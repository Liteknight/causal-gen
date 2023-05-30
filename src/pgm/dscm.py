import sys
import torch
import torch.nn as nn

from utils_pgm import check_nan
from datasets import get_attr_max_min

sys.path.append("..")


class DSCM(nn.Module):
    def __init__(self, args, pgm, predictor, vae):
        super().__init__()
        self.args = args
        self.pgm = pgm  # DAG model excluding x
        self.pgm.requires_grad_(False)
        self.predictor = predictor  # parent classifiers
        self.predictor.requires_grad_(False)
        self.vae = vae  # HVAE for x
        # lagrange multiplier
        self.lmbda = nn.Parameter(args.lmbda_init * torch.ones(1))
        self.register_buffer("eps", args.elbo_constraint * torch.ones(1))

    def forward(self, obs, do, elbo_fn, cf_particles=1, t_abduct=1.0):
        pa = {k: v for k, v in obs.items() if k != "x"}
        # forward vae with factual parents
        _pa = vae_preprocess(self.args, {k: v.clone() for k, v in pa.items()})
        vae_out = self.vae(obs["x"], _pa, beta=self.args.beta)

        if cf_particles > 1:  # for calculating counterfactual uncertainty
            cfs = {"x": torch.zeros_like(obs["x"])}
            cfs.update({"x2": torch.zeros_like(obs["x"])})

        for _ in range(cf_particles):
            # forward pgm, get counterfactual parents
            cf_pa = self.pgm.counterfactual(obs=pa, intervention=do, num_particles=1)
            _cf_pa = vae_preprocess(self.args, {k: v.clone() for k, v in cf_pa.items()})
            # forward vae with counterfactual parents
            zs = self.vae.abduct(obs["x"], parents=_pa, t=t_abduct)  # z ~ q(z|x,pa)
            cf_loc, cf_scale = self.vae.forward_latents(zs, parents=_cf_pa)
            rec_loc, rec_scale = self.vae.forward_latents(zs, parents=_pa)
            u = (obs["x"] - rec_loc) / rec_scale.clamp(min=1e-12)
            cf_x = torch.clamp(cf_loc + cf_scale * u, min=-1, max=1)

            if cf_particles > 1:
                cfs["x"] += cf_x
                with torch.no_grad():
                    cfs["x2"] += cf_x**2
            else:
                cfs = {"x": cf_x}

        # Var[X] = E[X^2] - E[X]^2
        if cf_particles > 1:
            with torch.no_grad():
                var_cf_x = (cfs["x2"] - cfs["x"] ** 2 / cf_particles) / cf_particles
                cfs.pop("x2", None)
            cfs["x"] = cfs["x"] / cf_particles
        else:
            var_cf_x = None

        cfs.update(cf_pa)
        if check_nan(vae_out) > 0 or check_nan(cfs) > 0:
            return {"loss": torch.tensor(float("nan"))}

        aux_loss = (
            elbo_fn.differentiable_loss(
                self.predictor.model_anticausal, self.predictor.guide_pass, **cfs
            )
            / cfs["x"].shape[0]
        )

        with torch.no_grad():
            sg = self.eps - vae_out["elbo"]
        damp = self.args.damping * sg
        loss = aux_loss - (self.lmbda - damp) * (self.eps - vae_out["elbo"])

        out = {}
        out.update(vae_out)
        out.update(
            {"loss": loss, "aux_loss": aux_loss, "cfs": cfs, "var_cf_x": var_cf_x}
        )
        return out


def vae_preprocess(args, pa):
    if "ukbb" in args.dataset:
        # preprocessing ukbb parents for the vae which was originally trained using
        # log standardized parents. The pgm was trained using [-1,1] normalization

        # first undo [-1,1] parent preprocessing back to original range
        for k, v in pa.items():
            if k != "mri_seq" and k != "sex":
                pa[k] = (v + 1) / 2  # [-1,1] -> [0,1]
                _max, _min = get_attr_max_min(k)
                pa[k] = pa[k] * (_max - _min) + _min

        # log_standardize parents for vae input
        for k, v in pa.items():
            logpa_k = torch.log(v.clamp(min=1e-12))
            if k == "age":
                pa[k] = (logpa_k - 4.112339973449707) / 0.11769197136163712
            elif k == "brain_volume":
                pa[k] = (logpa_k - 13.965583801269531) / 0.09537758678197861
            elif k == "ventricle_volume":
                pa[k] = (logpa_k - 10.345998764038086) / 0.43127763271331787
    # concatenate parents expand to input res for conditioning the vae
    pa = torch.cat(
        [pa[k] if len(pa[k].shape) > 1 else pa[k][..., None] for k in args.parents_x],
        dim=1,
    )
    pa = pa[..., None, None].repeat(1, 1, *(args.input_res,) * 2).cuda().float()
    return pa
