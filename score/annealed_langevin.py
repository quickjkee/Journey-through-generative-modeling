import torch


@torch.no_grad()
def sample_anneal_langevin(x, score_nn, sigmas, eps=6.2e-6, T=5):
    """
    Sampling procedure via annealed langevin dynamics
    :param x: (Tensor), [n_samples x C x W x H], initial samples
    :param score_nn: (nn.Module), score network
    :param T: (Int), number of steps for each level of noise
    :param sigmas: (Tensor), different levels of noise
    :param eps: (Float), step size
    :return: (Tensor), [n_samples x C x W x H]
    """
    samples = []

    for label, sigma in enumerate(sigmas):
        labels = torch.ones(size=(x.size(dim=0),), device=x.device) * label
        labels = labels.long()

        used_sigmas = sigmas[labels].view(-1, 1, 1, 1)
        step_size = eps * (sigma / sigmas[-1]) ** 2

        for t in range(T):
            samples.append(torch.clamp(x, -1.0, 1.0).to('cpu'))

            z = torch.randn_like(x, device=x.device) * torch.tensor(torch.sqrt(2 * step_size), device=x.device)

            score = score_nn(x, used_sigmas)
            x = x + step_size * score + z

    return samples
