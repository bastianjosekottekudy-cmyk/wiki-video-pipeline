# Cloud Auto-Deployment Rule for News Shorts & Wiki Shorts

Whenever you make code edits, configuration changes, or bug fixes to either `news-shorts-pipeline` or `wiki-video-pipeline`:

1. **Auto-Deploy to Cloud**: Run the deployment script to immediately sync the changes to the 24/7 cloud server:
   ```bash
   /home/bastianj/Projects/deploy-to-cloud.sh
   ```
   *(Or `/home/bastianj/Projects/deploy-to-cloud.sh news` or `/home/bastianj/Projects/deploy-to-cloud.sh wiki`).*

2. **Git Hook Support**: Any `git commit` in either repository will also trigger the background deployment hook automatically.

3. **Verify**: Ensure the cloud service status outputs `active (running)`.
