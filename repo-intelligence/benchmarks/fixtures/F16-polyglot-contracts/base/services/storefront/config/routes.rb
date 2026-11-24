Rails.application.routes.draw do
  get "/health", to: "status#show"
  resources :carts, only: [:show, :create]
end
