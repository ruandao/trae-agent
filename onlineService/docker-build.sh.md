将镜像推送到仓库:

1. 先 docker login --username=xxxx 仓库地址
2. 推送仓库
PUSH_IMAGE=1 IMAGE=registry.example.com/task2app-trae:latest ./onlineService/docker-build.sh

