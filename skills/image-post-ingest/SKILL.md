---
name: image-post-ingest
description: |
  Multimodal ingestion pipeline for posts containing images: OCR + captioning via Claude Vision.
  Use when: "analyze image post", "process image for misinformation", "OCR this image",
  "extract text from image", "caption this social media image"
version: 1.1.0
metadata: {"openclaw": {"requires": {"env": ["ANTHROPIC_API_KEY"]}, "primaryEnv": "ANTHROPIC_API_KEY"}}
allowed-tools: Bash, Write
---

# Skill: image-post-ingest

## Purpose
Full multimodal ingestion pipeline for posts containing images.
Runs OCR + image captioning via Claude Vision, generates embeddings,
and indexes the merged evidence in Chroma and Kuzu.

## Workspace
`ingestion`

## Trigger Conditions
Activate this skill when the user or planner agent asks to:
- Analyze an image post for misinformation
- Extract text from an image (OCR)
- Caption a social media image
- Process a multimodal post containing both text and images

## Step-by-Step Instructions

1. For each image in the post (or the provided `image_url` / `image_path`):
   a. Run Claude Vision via the Python service layer
   b. If OCR/captioning fails, degrade: continue with post text only; flag in run_logs
   c. Generate and store embedding from merged text

2. Write updated post data to the handoff file:
   ```
   workspaces/shared/handoffs/ingestion_out.json
   ```

## Input Format
```json
{
  "post": "Post object (id, text, images list)",
  "image_url": "string? — public URL to image",
  "image_path": "string? — local file path to image"
}
```

## Output Format
```json
{
  "post": "updated Post object with images populated",
  "image_results": [
    {
      "image_id": "string",
      "ocr_text": "string",
      "image_caption": "string",
      "image_type": "screenshot | chart | meme | photo | other",
      "candidate_claims": ["string"],
      "embedding_id": "string",
      "image_text_unavailable": false
    }
  ]
}
```

## Error Handling
| Error | Action |
|---|---|
| OCR returns empty | Degrade: use post_text only; set image_text_unavailable: true |
| Claude Vision API error | Degrade: log vision_api_error; continue with text only |
| Image URL unreachable | Log image_url_unreachable; skip image |
| Embedding API error | Log embedding_error; store post without embedding |
