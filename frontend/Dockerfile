FROM nginx:alpine

# Remove default nginx static files
RUN rm -rf /usr/share/nginx/html/*

# Copy entire frontend folder
COPY . /usr/share/nginx/html

EXPOSE 80
