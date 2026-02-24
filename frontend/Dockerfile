FROM nginx:alpine

# Remove default nginx static files
RUN rm -rf /usr/share/nginx/html/*

# Use project nginx config with gzip + cache headers
COPY default.conf /etc/nginx/conf.d/default.conf

# Copy entire frontend folder
COPY . /usr/share/nginx/html

EXPOSE 80
