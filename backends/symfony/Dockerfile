FROM php:8.4.25-cli-alpine3.24@sha256:1f044d6de9f2338df85f32719dfd2938e284f848a91b5ccee6a3d375ca81caa5

RUN apk add --no-cache libpq-dev postgresql18-client \
    && docker-php-ext-install pdo_pgsql

COPY --from=composer:2.8.12@sha256:5248900ab8b5f7f880c2d62180e40960cd87f60149ec9a1abfd62ac72a02577c /usr/bin/composer /usr/local/bin/composer

WORKDIR /srv/symfony
COPY backends/symfony/composer.json backends/symfony/composer.lock ./
RUN composer install --no-dev --no-interaction --prefer-dist
COPY backends/symfony/bin ./bin
COPY backends/symfony/config ./config
COPY backends/symfony/public ./public
COPY backends/symfony/src ./src
RUN mkdir -p var/log var/cache && php bin/console cache:warmup --env=prod

CMD ["php", "-S", "0.0.0.0:8000", "-t", "public", "public/index.php"]
