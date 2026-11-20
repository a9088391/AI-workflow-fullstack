FROM ruby:4.0.10-bookworm@sha256:5c8d4535998584fed1e33b12e19e702bb18ee69231dd2f713399e89d790716fb AS gems
WORKDIR /srv/rails
COPY backends/rails/Gemfile backends/rails/Gemfile.lock ./
RUN bundle config set frozen true && bundle install

FROM ruby:4.0.10-slim-bookworm@sha256:2ae2957252f1d59985f012f876f4f70d8fc643537766499ba5efa2c991319823
WORKDIR /srv/rails
COPY --from=gems /usr/local/bundle /usr/local/bundle
COPY backends/rails ./
ENV RAILS_ENV=production RAILS_LOG_TO_STDOUT=1
EXPOSE 8000
CMD ["bundle", "exec", "puma", "-b", "tcp://0.0.0.0:8000", "-t", "0:4", "config.ru"]
