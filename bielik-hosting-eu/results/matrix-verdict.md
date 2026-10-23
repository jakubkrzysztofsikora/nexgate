# Public Gemma / Bielik hosting — provider matrix (2026-10-22)

Scope: a public Gemma 4 12B 8-bit coop template. Mac Studio remains for Bielik/test only.

| Provider and region | GPU | Public price | Fit and caveats | Template decision |
|---|---|---|---|---|
| UpCloud, Helsinki, Finland | 1× L4, 24GB | €0.58/hour on-demand; €0.57/hour spot. €417.60 at 720 hours or €423.40 at 730 hours. | Dedicated L4, zero egress, Finland hosting. Google describes Gemma 4 12B as runnable with 16GB VRAM. The 8-bit vLLM checkpoint/quantization, tool-call behavior, and L4 queue capacity still need a real smoke and benchmark. | Best public-price candidate. Listed as public pre-registration only; not launchable yet. |
| CloudFerro, Warsaw WAW3-1, Poland | A6000 vGPU slices, 6/12/24/48GB | EUR ex-VAT: €0.51/€1.01/€2.03/€4.05 per hour. Monthly commitments for 6/12/24GB are €349.44/€698.89/€1,397.78; annual commitment monthly rates are €205.56/€411.11/€822.22. | Official table currency is EUR, not PLN. The 6GB slice is only a tight Bielik Q4 option; 12GB has no headroom for Gemma 8-bit; 24GB is the smallest plausible Gemma slice. The 2-vCPU 6GB tier will have slow prefill. | Poland alternative if Warsaw residency matters; 24GB is much dearer than UpCloud L4. |
| De Novo Tensor Cloud, Ukraine | H200 NVL, H100, A100 NVL, L40S, L4; whole or 1/2, 1/4, 1/8 slices | Quote only; the calculator asks for a tailored commercial proposal. | Public product supports GPU virtualization and commercial Kubernetes. Public cloud locations Kyiv/Lviv are documented, but exact Tensor Cloud GPU site, stock, price, and SLA need written confirmation. | Request a quote before listing. No public-priced SKU to publish yet. |
| OVHcloud AI Endpoints | Managed, per-token model catalog | Pay per token; model-specific prices | OpenAI-compatible tools API, but current catalog does not offer Gemma 4 12B or customer-uploaded weights. BGE multilingual Gemma2 is embeddings, not chat. | Not a Gemma host. Do not present as a Gemma template. |
| OVHcloud Public Cloud GPU, France/EU | L4 or L40S (48GB) self-hosted | Official product pages route exact prices through the calculator; region and stock need checking. | L40S 48GB is a plausible fit for self-hosted 8-bit Gemma. Requires an actual price/SKU quote and an engine/tool-call smoke. The older RTX 5000 16GB estimate is legacy and too tight for a 32K 8-bit workload. | Viable fallback after quote; not yet a priced CoopForge offer. |

## Recommendation

Use UpCloud Helsinki L4 as the candidate public Gemma provider. The current
`gemma-fi-l4` catalog row is visible to everyone but marked **soon**: pre-register
only. No UpCloud provisioning path, pinned 8-bit CUDA checkpoint, live tool-call
test, or L4 capacity benchmark exists yet. Do not change it to `live` until those
four gates pass.

For Bielik, the owned Mac Studio remains the lowest-cost host. CloudFerro's
`vm.a6000.1` is €0.51/hour for a 6GB vGPU slice, not €0.12/hour; the earlier
Poland artifact mislabeled EUR prices as PLN. The 6GB slice is a tight Q4 option,
not a 32K-context recommendation. UpCloud's 24GB L4 costs €0.58/hour and offers
more memory at modestly higher hourly cost.

## Sources

- [CloudFerro WAW3-1 GPU price table](https://www.cloudferro.com/pricing/pricing-tables/waw3-1-cloud/virtual-machines-vm-with-gpu/) — EUR ex-VAT, hourly and commitment prices.
- [UpCloud GPU server plans](https://upcloud.com/products/gpu-servers/) — L4 24GB prices, Finland Helsinki offering, zero-egress terms.
- [De Novo Tensor Cloud](https://denovo.ua/en/tensor-cloud) and [pricing](https://denovo.ua/en/pricing) — GPU catalog and quote-only pricing.
- [OVHcloud AI Endpoints catalog](https://www.ovhcloud.com/en/public-cloud/ai-endpoints/catalog/) — managed model catalog and tool-capability filter.
- [OVHcloud L40S GPU page](https://www.ovhcloud.com/en/public-cloud/gpu/l40s/) — L40S 48GB and price-calculator link.
- [Google Gemma 4 12B developer guide](https://developers.googleblog.com/gemma-4-12b-the-developer-guide/) — local 16GB VRAM guidance and supported serving ecosystems.
- [vLLM Gemma 4 guide](https://docs.vllm.ai/projects/recipes/en/stable/Google/Gemma4.html) — vLLM Gemma 4 serving and tool calling; 12B 8-bit on an L4 still needs validation.
